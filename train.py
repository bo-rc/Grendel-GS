#
# Copyright (C) 2023, Inria
# GRAPHDECO research group, https://team.inria.fr/graphdeco
# All rights reserved.
#
# This software is free for non-commercial, research and evaluation use 
# under the terms of the LICENSE.md file.
#
# For inquiries contact  george.drettakis@inria.fr
#

import os
import torch
import numpy as np
import json
from random import randint
from utils.loss_utils import l1_loss
from gaussian_renderer import render, network_gui
from gaussian_renderer.image_distribution import distributed_loss_computation, replicated_loss_computation
import sys
from scene import Scene, GaussianModel
# from scene.workload_division import DivisionStrategy, DivisionStrategyHistory_1, OldDivisionStrategyHistory, WorkloadDivisionTimer, get_evenly_global_strategy_str
from scene.workload_division import DivisionStrategy_1, DivisionStrategyHistory_1, DivisionStrategyHistory_2, DivisionStrategyWS1, DivisionStrategyManuallySet, WorkloadDivisionTimer, get_evenly_global_strategy_str
from utils.general_utils import safe_state, init_distributed
import utils.general_utils as utils
from utils.timer import Timer
import uuid
from tqdm import tqdm
from utils.image_utils import psnr
from argparse import ArgumentParser, Namespace
from arguments import ModelParams, PipelineParams, OptimizationParams, print_all_args
import time
try:
    from torch.utils.tensorboard import SummaryWriter
    TENSORBOARD_FOUND = True
except ImportError:
    TENSORBOARD_FOUND = False
import torch.distributed as dist

def training(dataset, opt, pipe, args, log_file):
    (testing_iterations,
     saving_iterations, 
     checkpoint_iterations, 
     checkpoint, 
     debug_from, 
     fixed_training_image, 
     disable_auto_densification) = (
        args.test_iterations,
        args.save_iterations,
        args.checkpoint_iterations,
        args.start_checkpoint,
        args.debug_from,
        args.fixed_training_image,
        args.disable_auto_densification
    )

    timers = Timer(args)
    utils.set_timers(timers)
    utils.set_log_file(log_file)
    utils.set_cur_iter(0)

    first_iter = 0
    tb_writer = prepare_output_and_logger(dataset)
    gaussians = GaussianModel(dataset.sh_degree)

    with torch.no_grad():
        scene = Scene(dataset, gaussians)
        if args.duplicate_gs_cnt > 0:
            gaussians.duplicate_gaussians(args.duplicate_gs_cnt)
        gaussians.training_setup(opt)

    if checkpoint:
        assert not args.memory_distribution, "memory_distribution does not support checkpoint yet!"
        (model_params, first_iter) = torch.load(checkpoint, map_location=torch.device("cuda", utils.LOCAL_RANK))
        gaussians.restore(model_params, opt)
        print("rank {} Restored from checkpoint: {}, at iteration {}".format(utils.LOCAL_RANK, checkpoint, first_iter))
        log_file.write("rank {} Restored from checkpoint: {}, at iteration {}\n".format(utils.LOCAL_RANK, checkpoint, first_iter))

    bg_color = [1, 1, 1] if dataset.white_background else [0, 0, 0]
    background = torch.tensor(bg_color, dtype=torch.float32, device="cuda")

    iter_start = torch.cuda.Event(enable_timing = True)
    iter_end = torch.cuda.Event(enable_timing = True)

    # Print shape of gaussians parameters. 
    log_file.write("xyz shape: {}\n".format(gaussians._xyz.shape))
    log_file.write("f_dc shape: {}\n".format(gaussians._features_dc.shape))
    log_file.write("f_rest shape: {}\n".format(gaussians._features_rest.shape))
    log_file.write("opacity shape: {}\n".format(gaussians._opacity.shape))
    log_file.write("scaling shape: {}\n".format(gaussians._scaling.shape))
    log_file.write("rotation shape: {}\n".format(gaussians._rotation.shape))


    viewpoint_stack = None
    ema_loss_for_log = 0.0
    progress_bar = tqdm(range(first_iter, opt.iterations), desc="Training progress")
    first_iter += 1

    # init epoch stats
    epoch_loss = 0
    epoch_id = 0
    epoch_progress_cnt = 0
    epoch_camera_size = len(scene.getTrainCameras())

    # init i2jsend_size file
    if args.save_i2jsend:
        i2jsend_file = open(args.log_folder+"/i2jsend_ws="+str(utils.WORLD_SIZE)+"_rk="+str(utils.LOCAL_RANK)+".txt", 'w')

    # init workload division strategy stuff
    cameraId2StrategyHistory = {}
    adjust_div_stra_timer = WorkloadDivisionTimer() if args.adjust_div_stra else None

    utils.check_memory_usage_logging("after init and before training loop")    

    train_start_time = time.time()

    for iteration in range(first_iter, opt.iterations + 1):        
        if False:# disable network_gui for now
            if network_gui.conn == None:
                network_gui.try_connect()
            while network_gui.conn != None:
                try:
                    net_image_bytes = None
                    custom_cam, do_training, pipe.convert_SHs_python, pipe.compute_cov3D_python, keep_alive, scaling_modifer = network_gui.receive()
                    if custom_cam != None:
                        net_image = render(custom_cam, gaussians, pipe, background, scaling_modifer)["render"]
                        net_image_bytes = memoryview((torch.clamp(net_image, min=0, max=1.0) * 255).byte().permute(1, 2, 0).contiguous().cpu().numpy())
                    network_gui.send(net_image_bytes, dataset.source_path)
                    if do_training and ((iteration < int(opt.iterations)) or not keep_alive):
                        break
                except Exception as e:
                    network_gui.conn = None


        # Step Initialization
        utils.set_cur_iter(iteration)
        timers.clear()
        timers.start("pre_forward")
        iter_start.record()
        gaussians.update_learning_rate(iteration)
        # Every 1000 its we increase the levels of SH up to a maximum degree
        if iteration % 1000 == 0:
            gaussians.oneupSHdegree()
        # DEBUG: understand time for one cuda synchronize call.
        # timers.start("test_cuda_synchronize_time")
        # timers.stop("test_cuda_synchronize_time")


        # Prepare arguments for rendering cuda code. The values should all be string.
        cuda_args = {
            "mode": "train",
            "world_size": str(utils.WORLD_SIZE),
            "local_rank": str(utils.LOCAL_RANK),
            "log_folder": args.log_folder,
            "log_interval": str(args.log_interval),
            "iteration": str(iteration),
            "zhx_debug": str(args.zhx_debug),
            "zhx_time": str(args.zhx_time),
            "dist_division_mode": args.dist_division_mode,
            "stats_collector": {}
        }



        # Prepara data: Pick a random Camera
        if not viewpoint_stack:
            log_file.write("reset viewpoint stack\n")
            viewpoint_stack = scene.getTrainCameras().copy()
        viewpoint_cam = None
        if fixed_training_image == -1:
            camera_id = randint(0, len(viewpoint_stack)-1)
            viewpoint_cam = viewpoint_stack.pop(camera_id)
        else:
            viewpoint_cam = viewpoint_stack[fixed_training_image]
        utils.set_img_size(viewpoint_cam.image_height, viewpoint_cam.image_width)


        # Prepare Workload division strategy
        if args.adjust_div_stra:
            if args.adjust_mode == "1":
                
                if viewpoint_cam.uid not in cameraId2StrategyHistory:
                    cameraId2StrategyHistory[viewpoint_cam.uid] = DivisionStrategyHistory_1(viewpoint_cam, utils.WORLD_SIZE, utils.LOCAL_RANK, args.adjust_mode)
                strategy_history = cameraId2StrategyHistory[viewpoint_cam.uid]
                strategy = strategy_history.start_strategy()
                cuda_args["dist_global_strategy"] = strategy.get_gloabl_strategy_str()

            elif args.adjust_mode == "2":

                if viewpoint_cam.uid not in cameraId2StrategyHistory:
                    cameraId2StrategyHistory[viewpoint_cam.uid] = DivisionStrategyHistory_2(viewpoint_cam,
                                                                                            utils.WORLD_SIZE,
                                                                                            utils.LOCAL_RANK,
                                                                                            args.adjust_mode,
                                                                                            args.heuristic_decay) # TODO: tune this. 
                strategy_history = cameraId2StrategyHistory[viewpoint_cam.uid]
                strategy = strategy_history.start_strategy()
                cuda_args["dist_global_strategy"] = strategy.get_gloabl_strategy_str()

            elif args.adjust_mode == "3":
                tile_x = (viewpoint_cam.image_width + utils.BLOCK_X - 1) // utils.BLOCK_X
                tile_y = (viewpoint_cam.image_height + utils.BLOCK_Y - 1) // utils.BLOCK_Y
                strategy = DivisionStrategyManuallySet(
                    viewpoint_cam,
                    utils.WORLD_SIZE,
                    utils.LOCAL_RANK,
                    tile_x,
                    tile_y,
                    args.dist_global_strategy
                )
                cuda_args["dist_global_strategy"] = strategy.get_gloabl_strategy_str()
            else:
                assert False, "not implemented yet."
                # timers.start("pre_forward_adjust_div_stra")
                # if viewpoint_cam.uid not in cameraId2StrategyHistory:
                #     cameraId2StrategyHistory[viewpoint_cam.uid] = OldDivisionStrategyHistory(viewpoint_cam, utils.WORLD_SIZE, utils.LOCAL_RANK, args.adjust_mode)
                # strategy_history = cameraId2StrategyHistory[viewpoint_cam.uid]
                # division_strategy = strategy_history.get_next_strategy()
                # cuda_args["dist_division_mode"] = division_strategy.local_strategy_str
                # # TODO: improve it; now the format is just `T:$l,$r`, an example: `T:0,62`
                # cuda_args["dist_global_strategy"] = division_strategy.global_strategy_str
                # # format: 0,8,19,...,num_tiles
                # # TODO: many hacks for the simultaneous use of division_strategy and memory_distribution; It is not beautiful. to be optimized.
                # timers.stop("pre_forward_adjust_div_stra")
        else:
            tile_x = (viewpoint_cam.image_width + utils.BLOCK_X - 1) // utils.BLOCK_X
            tile_y = (viewpoint_cam.image_height + utils.BLOCK_Y - 1) // utils.BLOCK_Y
            strategy = DivisionStrategyWS1(viewpoint_cam, utils.WORLD_SIZE, utils.LOCAL_RANK, tile_x, tile_y)



        # Render
        if (iteration - 1) == debug_from:
            pipe.debug = True

        bg = torch.rand((3), device="cuda") if opt.random_background else background
        timers.stop("pre_forward")

        # NOTE: this is to make sure: we are measuring time for local work.
        # where to add this barrier depends on: whether there will be global communication(i.e. allreduce) in the following code.
        if utils.check_enable_python_timer() and utils.WORLD_SIZE > 1:
            torch.distributed.barrier()

        # Forward
        timers.start("forward")
        render_pkg = render(viewpoint_cam, gaussians, pipe, bg,
                            adjust_div_stra_timer=adjust_div_stra_timer,
                            cuda_args=cuda_args,
                            timers=timers,
                            strategy=strategy)
        timers.stop("forward")
        (image, 
         viewspace_point_tensor, 
         visibility_filter, 
         radii, 
         n_render,
         n_consider,
         n_contrib) = (
            render_pkg["render"],
            render_pkg["viewspace_points"], 
            render_pkg["visibility_filter"],
            render_pkg["radii"], 
            render_pkg["n_render"], 
            render_pkg["n_consider"], 
            render_pkg["n_contrib"]
        )
        if args.memory_distribution:
            i2j_send_size = render_pkg["i2j_send_size"]
            compute_locally = render_pkg["compute_locally"]


        if utils.check_enable_python_timer() and utils.WORLD_SIZE > 1:
            torch.distributed.barrier()


        # Loss Computation
        if args.image_distribution:
            # Distributed Loss Computation
            Ll1, ssim_loss = distributed_loss_computation(image, viewpoint_cam, compute_locally)
            loss = (1.0 - opt.lambda_dssim) * Ll1 + opt.lambda_dssim * (1.0 - ssim_loss)

        else:
            # Replicated Loss Computation
            Ll1, ssim_loss = replicated_loss_computation(image, viewpoint_cam)
            loss = (1.0 - opt.lambda_dssim) * Ll1 + opt.lambda_dssim * (1.0 - ssim_loss)
        utils.check_memory_usage_logging("after loss")


        # Logging
        log_file.write("iteration {} image: {} loss: {}\n".format(iteration, viewpoint_cam.image_name, loss.item()))
        epoch_progress_cnt = epoch_progress_cnt + 1
        epoch_loss = epoch_loss + loss.item()
        if epoch_progress_cnt == epoch_camera_size:
            assert args.fixed_training_image or viewpoint_stack == None or len(viewpoint_stack) == 0, \
                "viewpoint_stack should be empty at the end of epoch."
            log_file.write("epoch {} loss: {}\n".format(epoch_id, epoch_loss/epoch_progress_cnt))
            epoch_id = epoch_id + 1
            epoch_progress_cnt = 0
            epoch_loss = 0


        if utils.check_enable_python_timer() and utils.WORLD_SIZE > 1:
            torch.distributed.barrier()


        # Backward
        timers.start("backward")
        if args.adjust_div_stra:
            adjust_div_stra_timer.start("backward")
        loss.backward()
        if args.adjust_div_stra:
            adjust_div_stra_timer.stop("backward")
        timers.stop("backward")
        utils.check_memory_usage_logging("after backward")


        if utils.check_enable_python_timer() and utils.WORLD_SIZE > 1:
            torch.distributed.barrier()


        # Adjust workload division strategy. 
        if args.adjust_div_stra:
            if args.adjust_mode == "1":
                strategy_history.finish_strategy()
            elif args.adjust_mode == "2":
                # print(cuda_args["stats_collector"])

                if iteration > 20:# If we pass the warmup period.
                    strategy.update_stats(cuda_args["stats_collector"]["backward_render_time"])
                    strategy_history.finish_strategy()
            elif args.adjust_mode == "history_heuristic":
                assert False, "not implemented yet."
                # forward_time = adjust_div_stra_timer.elapsed("forward")
                # backward_time = adjust_div_stra_timer.elapsed("backward")

                # timers.start("post_forward_adjust_div_stra_synchronize_time")
                # forward_time, backward_time = DivisionStrategy.synchronize_time(utils.WORLD_SIZE, utils.LOCAL_RANK, forward_time, backward_time)
                # timers.stop("post_forward_adjust_div_stra_synchronize_time")

                # timers.start("post_forward_adjust_div_stra_synchronize_stats")
                # n_render, n_consider, n_contrib = DivisionStrategy.synchronize_stats(n_render, n_consider, n_contrib, timers)
                # timers.stop("post_forward_adjust_div_stra_synchronize_stats")

                # timers.start("post_forward_adjust_div_stra_update_result")
                # division_strategy.update_result(n_render, n_consider, n_contrib, forward_time, backward_time)
                # strategy_history.add(iteration, division_strategy)
                # timers.stop("post_forward_adjust_div_stra_update_result")
            elif args.adjust_mode == "none":
                assert False, "not implemented yet."
                # strategy_history.add(iteration, division_strategy)
                pass


        if utils.check_enable_python_timer() and utils.WORLD_SIZE > 1:
            torch.distributed.barrier()


        # Sync Gradients. NOTE: do not sync grad in args.memory_distribution mode
        if not args.memory_distribution:
            timers.start("sync_gradients")
            sparse_ids_mask = gaussians.sync_gradients(viewspace_point_tensor, utils.WORLD_SIZE)
            non_zero_indices_cnt = sparse_ids_mask.sum().item()
            total_indices_cnt = sparse_ids_mask.shape[0]
            log_file.write("iteration {} non_zero_indices_cnt: {} total_indices_cnt: {} ratio: {}\n".format(iteration, non_zero_indices_cnt, total_indices_cnt, non_zero_indices_cnt/total_indices_cnt))
            timers.stop("sync_gradients")


        if utils.check_enable_python_timer() and utils.WORLD_SIZE > 1:
            torch.distributed.barrier()


        iter_end.record()


        with torch.no_grad():
            # Progress bar
            ema_loss_for_log = 0.4 * loss.item() + 0.6 * ema_loss_for_log

            if utils.LOCAL_RANK == 0:
                if iteration % 10 == 0:
                    progress_bar.set_postfix({"Loss": f"{ema_loss_for_log:.{7}f}"})
                    progress_bar.update(10)
                if iteration == opt.iterations:
                    progress_bar.close()

            # Log and save
            training_report(tb_writer, iteration, Ll1, loss, l1_loss, iter_start.elapsed_time(iter_end), testing_iterations, scene, render, (pipe, background), log_file)
            if iteration in saving_iterations: # Do not check rk here. Because internal implementation maybe distributed save.
                print("\n[ITER {}] Saving Gaussians".format(iteration))
                log_file.write("[ITER {}] Saving Gaussians\n".format(iteration))
                scene.save(iteration)

            # Densification
            if not disable_auto_densification and iteration < opt.densify_until_iter:
                # Keep track of max radii in image-space for pruning
                timers.start("densification")

                timers.start("densification_update_stats")
                gaussians.max_radii2D[visibility_filter] = torch.max(gaussians.max_radii2D[visibility_filter], radii[visibility_filter])
                gaussians.add_densification_stats(viewspace_point_tensor, visibility_filter)
                timers.stop("densification_update_stats")

                if iteration > opt.densify_from_iter and iteration % opt.densification_interval == 0:
                    assert args.stop_update_param == False, "stop_update_param must be false for densification; because it is a flag for debugging."

                    timers.start("densify_and_prune")
                    size_threshold = 20 if iteration > opt.opacity_reset_interval else None
                    gaussians.densify_and_prune(opt.densify_grad_threshold, 0.005, scene.cameras_extent, size_threshold)
                    timers.stop("densify_and_prune")

                    memory_usage = torch.cuda.memory_allocated() / 1024 / 1024 / 1024
                    max_memory_usage = torch.cuda.max_memory_allocated() / 1024 / 1024 / 1024
                    log_file.write("iteration {} densify_and_prune. Now num of 3dgs: {}. Now Memory usage: {} GB. Max Memory usage: {} GB. \n".format(
                        iteration, gaussians.get_xyz.shape[0], memory_usage, max_memory_usage))
                
                if iteration % opt.opacity_reset_interval == 0 or (dataset.white_background and iteration == opt.densify_from_iter):
                    timers.start("reset_opacity")
                    gaussians.reset_opacity()
                    timers.stop("reset_opacity")

                timers.stop("densification")

            # Optimizer step
            if iteration < opt.iterations:
                timers.start("optimizer_step")
                if not args.stop_update_param:
                    gaussians.optimizer.step()
                gaussians.optimizer.zero_grad(set_to_none = True)
                timers.stop("optimizer_step")
                utils.check_memory_usage_logging("after optimizer step")

            if utils.LOCAL_RANK == 0 and (iteration in checkpoint_iterations): #TODO: have not handled args.memory_distribution yet.
                print("\n[ITER {}] Saving Checkpoint".format(iteration))
                log_file.write("[ITER {}] Saving Checkpoint\n".format(iteration))
                torch.save((gaussians.capture(), iteration), scene.model_path + "/chkpnt" + str(iteration) + ".pth")
    
        # Finish a iteration and clean up
        if utils.check_enable_python_timer():
            timers.printTimers(iteration)

        log_file.flush()

        if args.save_i2jsend and iteration % args.log_interval == 1:
            i2jsend_file.write("iteration {}:{}\n".format(iteration, json.dumps(i2j_send_size)))
            i2jsend_file.flush()


    # Finish training
    if args.adjust_div_stra:

        if args.adjust_mode in ["1", "2"]:
            data_json = {}
            for camera_id, strategy_history in cameraId2StrategyHistory.items():
                data_json[camera_id] = strategy_history.to_json()
            
            with open(args.log_folder+"/strategy_history_ws="+str(utils.WORLD_SIZE)+"_rk="+str(utils.LOCAL_RANK)+".json", 'w') as f:
                json.dump(data_json, f)
        elif args.adjust_mode == "none":
            pass

    if args.end2end_time:
        torch.cuda.synchronize()
        log_file.write("end2end total_time: {:.6f} ms, iterations: {}, throughput {:.2f} it/s\n".format(time.time() - train_start_time, opt.iterations, opt.iterations/(time.time() - train_start_time)))
    
    log_file.write("Max Memory usage: {} GB.\n".format(torch.cuda.max_memory_allocated() / 1024 / 1024 / 1024))

def prepare_output_and_logger(args):    
    if not args.model_path:
        if os.getenv('OAR_JOB_ID'):
            unique_str=os.getenv('OAR_JOB_ID')
        else:
            unique_str = str(uuid.uuid4())
        args.model_path = os.path.join("./output/", unique_str[0:10])

    # Set up output folder
    if utils.LOCAL_RANK != 0:
        return None
    print("Output folder: {}".format(args.model_path))
    os.makedirs(args.model_path, exist_ok = True)
    with open(os.path.join(args.model_path, "cfg_args"), 'w') as cfg_log_f:
        cfg_log_f.write(str(Namespace(**vars(args))))

    # Create Tensorboard writer
    tb_writer = None
    if TENSORBOARD_FOUND:
        tb_writer = SummaryWriter(args.model_path)
    else:
        print("Tensorboard not available: not logging progress")
    return tb_writer

def training_report(tb_writer, iteration, Ll1, loss, l1_loss, elapsed, testing_iterations, scene : Scene, renderFunc, renderArgs, log_file):
    if tb_writer:
        tb_writer.add_scalar('train_loss_patches/l1_loss', Ll1.item(), iteration)
        tb_writer.add_scalar('train_loss_patches/total_loss', loss.item(), iteration)
        tb_writer.add_scalar('iter_time', elapsed, iteration)

    # Report test and samples of training set
    if iteration in testing_iterations:
        torch.cuda.empty_cache()
        validation_configs = ({'name': 'test', 'cameras' : scene.getTestCameras()}, 
                              {'name': 'train', 'cameras' : [scene.getTrainCameras()[idx % len(scene.getTrainCameras())] for idx in range(5, 30, 5)]})

        # prepare arguments for render cuda code.
        cuda_args = {
            "mode": "test",
            "world_size": str(utils.WORLD_SIZE),
            "local_rank": str(utils.LOCAL_RANK),
            "log_folder": args.log_folder,
            "log_interval": str(args.log_interval),
            "iteration": str(iteration),# this is training iteration, not testing iteration.
            "zhx_debug": str(args.zhx_debug),
            "zhx_time": str(args.zhx_time),
            "dist_division_mode": "tile_num",# during testing, we does not have statistics to adjust workload division strategy.
        }
        renderKwargs = {"scaling_modifier": 1.0, "override_color": None, "adjust_div_stra_timer": None, "cuda_args": cuda_args}

        for config in validation_configs:
            if config['cameras'] and len(config['cameras']) > 0:
                l1_test = 0.0
                psnr_test = 0.0
                for idx, viewpoint in enumerate(config['cameras']):
                    cuda_args["dist_global_strategy"] = get_evenly_global_strategy_str(viewpoint)# HACK: Use naive distribution strategy during testing.
                    hack_history = DivisionStrategyHistory_1(viewpoint, utils.WORLD_SIZE, utils.LOCAL_RANK, args.adjust_mode)# HACK
                    renderKwargs["strategy"] = hack_history.start_strategy()# HACK: Use naive distribution strategy during testing.
                    image = renderFunc(viewpoint, scene.gaussians, *renderArgs, **renderKwargs)["render"]
                    if utils.WORLD_SIZE > 1:
                        torch.distributed.all_reduce(image, op=dist.ReduceOp.SUM)
                    image = torch.clamp(image, 0.0, 1.0)
                    gt_image = torch.clamp(viewpoint.original_image.to("cuda"), 0.0, 1.0)
                    if tb_writer and (idx < 5):
                        tb_writer.add_images(config['name'] + "_view_{}/render".format(viewpoint.image_name), image[None], global_step=iteration)
                        if iteration == testing_iterations[0]:
                            tb_writer.add_images(config['name'] + "_view_{}/ground_truth".format(viewpoint.image_name), gt_image[None], global_step=iteration)
                    l1_test += l1_loss(image, gt_image).mean().double()
                    psnr_test += psnr(image, gt_image).mean().double()
                psnr_test /= len(config['cameras'])
                l1_test /= len(config['cameras'])          
                print("\n[ITER {}] rank {} Evaluating {}: L1 {} PSNR {}".format(iteration, utils.LOCAL_RANK, config['name'], l1_test, psnr_test))
                log_file.write("[ITER {}] Evaluating {}: L1 {} PSNR {}\n".format(iteration, config['name'], l1_test, psnr_test))

                if tb_writer:
                    tb_writer.add_scalar(config['name'] + '/loss_viewpoint - l1_loss', l1_test, iteration)
                    tb_writer.add_scalar(config['name'] + '/loss_viewpoint - psnr', psnr_test, iteration)

        if tb_writer:
            tb_writer.add_histogram("scene/opacity_histogram", scene.gaussians.get_opacity, iteration)
            tb_writer.add_scalar('total_points', scene.gaussians.get_xyz.shape[0], iteration)
        torch.cuda.empty_cache()

if __name__ == "__main__":
    # Set up command line argument parser
    parser = ArgumentParser(description="Training script parameters")
    lp = ModelParams(parser)
    op = OptimizationParams(parser)
    pp = PipelineParams(parser)
    parser.add_argument('--ip', type=str, default="127.0.0.1")
    parser.add_argument('--port', type=int, default=6009)
    parser.add_argument('--debug_from', type=int, default=-1)
    parser.add_argument('--detect_anomaly', action='store_true', default=False)
    parser.add_argument("--test_iterations", nargs="+", type=int, default=[7_000, 30_000])
    parser.add_argument("--save_iterations", nargs="+", type=int, default=[7_000, 30_000])
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--checkpoint_iterations", nargs="+", type=int, default=[])
    parser.add_argument("--start_checkpoint", type=str, default = None)
    # TODO: restart from a checkpoint will give a different result because of training image orders are different.
    # I should make the order the same later.
    parser.add_argument("--log_folder", type=str, default = "logs")
    parser.add_argument("--zhx_debug", action='store_true', default=False)
    parser.add_argument("--zhx_time", action='store_true', default=False)
    parser.add_argument("--zhx_python_time", action='store_true', default=False)
    parser.add_argument("--log_interval", type=int, default=50)
    parser.add_argument("--fixed_training_image", type=int, default=-1)
    parser.add_argument("--disable_auto_densification", action='store_true', default=False)
    parser.add_argument("--global_timer", action='store_true', default=False)
    parser.add_argument("--end2end_time", action='store_true', default=False)
    parser.add_argument("--dist_division_mode", type=str, default="tile_num")
    parser.add_argument("--stop_update_param", action='store_true', default=False)
    parser.add_argument("--duplicate_gs_cnt", type=int, default=0)
    parser.add_argument("--adjust_div_stra", action='store_true', default=False)
    parser.add_argument("--adjust_mode", type=str, default="heuristic")# none, history_heuristic, 
    parser.add_argument("--dist_global_strategy", type=str, default="")
    parser.add_argument("--lazy_load_image", action='store_true', default=False) # lazily move image to gpu.
    parser.add_argument("--memory_distribution", action='store_true', default=False) # distribute memory in distributed training.
    parser.add_argument("--force_python_timer_iterations", nargs="+", type=int, default=[600, 700, 800]) # print timers at these iterations. 600 is the default first densification iteration.
    parser.add_argument("--save_i2jsend", action='store_true', default=False) # save i2jsend_size to file.
    parser.add_argument("--time_image_loading", action='store_true', default=False) # time image loading.
    parser.add_argument("--check_memory_usage", action='store_true', default=False) # check memory usage.
    parser.add_argument("--image_distribution", action='store_true', default=False)
    parser.add_argument("--heuristic_decay", type=float, default=0)
    parser.add_argument("--disable_checkpoint_and_save", action='store_true', default=False)
    args = parser.parse_args(sys.argv[1:])
    args.save_iterations.append(args.iterations)

    ## Prepare arguments.
    # Set up global args
    utils.set_args(args)

    # Set up distributed training
    init_distributed()
    print("Local rank: " + str(utils.LOCAL_RANK) + " World size: " + str(utils.WORLD_SIZE))

    # Check arguments
    if args.adjust_div_stra and utils.WORLD_SIZE == 1:
        print("adjust_div_stra is enabled, but WORLD_SIZE is 1. disable adjust_div_stra.")
        args.adjust_div_stra = False
    assert not (args.memory_distribution and utils.WORLD_SIZE == 1), "memory_distribution needs WORLD_SIZE > 1!"
    assert not (args.memory_distribution and len(args.checkpoint_iterations)>0 ), "memory_distribution does not support checkpoint yet!"
    assert not (args.memory_distribution and not args.adjust_div_stra), "has not implement memory_distribution \
        without args.adjust_div_stra flag. Could use adjust_mode=none to enable naive tile_num based adjustment."
    assert not (args.save_i2jsend and not args.memory_distribution), "save_i2jsend needs memory_distribution!"
    assert not (args.image_distribution and not args.memory_distribution), "image_distribution needs memory_distribution!"
    assert not (args.image_distribution and utils.WORLD_SIZE == 1), "image_distribution needs WORLD_SIZE > 1!"
    assert not (args.adjust_div_stra and args.adjust_mode == "3" and args.dist_global_strategy == ""), "dist_global_strategy must be set if adjust_mode is 3."
    assert not (args.adjust_div_stra and args.adjust_mode == "3" and args.fixed_training_image == -1), "fixed_training_image must be set if adjust_mode is 3."
    assert not (args.adjust_div_stra and args.adjust_mode == "3" and not args.stop_update_param), "stop_update_param must be set if adjust_mode is 3."

    if args.fixed_training_image != -1:
        args.test_iterations = [] # disable testing during training.
        args.disable_auto_densification = True

    if args.disable_checkpoint_and_save:
        print("Attention! disable_checkpoint_and_save is enabled. disable checkpoint and save.")
        args.checkpoint_iterations = []
        args.save_iterations = []

    # create log folder
    if utils.LOCAL_RANK == 0:
        os.makedirs(args.log_folder, exist_ok = True)
        os.makedirs(args.model_path, exist_ok = True)

    if utils.WORLD_SIZE > 1:
        torch.distributed.barrier()# make sure log_folder is created before other ranks start writing log.

    # Initialize system state (RNG)
    safe_state(args.quiet)

    # Start GUI server, configure and run training 
    if False:# disable network_gui for now
        network_gui.init(args.ip, args.port+utils.LOCAL_RANK)
    torch.autograd.set_detect_anomaly(args.detect_anomaly)

    # Initialize log file and print all args
    log_file = open(args.log_folder+"/python_ws="+str(utils.WORLD_SIZE)+"_rk="+str(utils.LOCAL_RANK)+".log", 'w')
    print_all_args(args, log_file)

    training(lp.extract(args), op.extract(args), pp.extract(args), args, log_file)

    # All done
    print("\nTraining complete.")