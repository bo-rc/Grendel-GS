

# bash /global/homes/j/jy-nyu/gaussian-splatting/building_scripts/bui2k_1g_test.sh


# source ~/zhx.sh

# head_node_ip="nid003080"
# port=27709
# rid=104

expe_name="mat_sm_ae_ball_1g_test1"

# echo "connecting to head_node_ip: $head_node_ip, port: $port"

# export FI_MR_CACHE_MONITOR=disabled

# bash /mat_sm_ae_ball_1g_test1.sh

python /global/homes/j/jy-nyu/gaussian-splatting/train.py \
    -s /pscratch/sd/j/jy-nyu/datasets/matrixcity_small/bdaibdai___MatrixCity/small_city/aerial/pose/block_all_my \
    --iterations 3000 \
    --log_interval 250 \
    --log_folder experiments/cnt0_2 \
    --model_path experiments/cnt0_2 \
    --dp_size 1 \
    --bsz 1 \
    --densify_until_iter 15000 \
    --densify_grad_threshold 0.0002 \
    --percent_dense 0.01 \
    --opacity_reset_interval 3000 \
    --zhx_python_time \
    --log_iteration_memory_usage \
    --check_memory_usage \
    --end2end_time \
    --test_iterations 200 7000 15000 20000 30000 40000 50000 60000 70000 80000 90000 100000 110000 120000 130000 140000 150000 160000 170000 180000 190000 200000 \
    --save_iterations 7000 30000 50000 80000 100000 120000 150000 170000 200000 \
    --checkpoint_iterations 7000 30000 50000 70000 85000 100000 120000 150000 170000 200000 \
    --check_cpu_memory \
    --stop_update_param \
    --densify_from_iter 6000


    # --distributed_dataset_storage \




