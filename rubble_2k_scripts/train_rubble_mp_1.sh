
# cp -r /pscratch/sd/j/jy-nyu/datasets/mill19/rubble-1.5k /tmp/rubble-1.5k

# echo "finish copying dataset."

torchrun --standalone --nnodes=1 --nproc-per-node=4 train.py \
    -s /pscratch/sd/j/jy-nyu/datasets/mill19/rubble-2k \
    --iterations 200000 \
    --log_interval 250 \
    --log_folder /pscratch/sd/j/jy-nyu/running_expes/rubble_2k_mp_1 \
    --model_path /pscratch/sd/j/jy-nyu/running_expes/rubble_2k_mp_1 \
    --redistribute_gaussians_mode "1" \
    --gaussians_distribution \
    --image_distribution_mode "3" \
    --dp_size 1 \
    --bsz 1 \
    --zhx_python_time \
    --log_iteration_memory_usage \
    --check_memory_usage \
    --end2end_time \
    --test_iterations 200 7000 15000 20000 30000 40000 50000 60000 70000 80000 90000 100000 130000 150000 180000 200000 \
    --save_iterations 7000 30000 50000 80000 100000 150000 200000 \

# torchrun --standalone --nnodes=1 --nproc-per-node=4 train.py \
#     -s /pscratch/sd/j/jy-nyu/datasets/mill19/rubble-pixsfm-from-fred \
#     --iterations 80000 \
#     --log_interval 250 \
#     --log_folder /pscratch/sd/j/jy-nyu/running_expes/rubble_mp4_7_sbc \
#     --model_path /pscratch/sd/j/jy-nyu/running_expes/rubble_mp4_7_sbc \
#     --redistribute_gaussians_mode "1" \
#     --gaussians_distribution \
#     --image_distribution_mode "3" \
#     --dp_size 1 \
#     --bsz 1 \
#     --densify_grad_threshold 0.00005 \
#     --percent_dense 0.002 \
#     --zhx_python_time \
#     --densify_until_iter 40000 \
#     --log_iteration_memory_usage \
#     --check_memory_usage \
#     --end2end_time \
#     --test_iterations 200 7000 15000 20000 30000 40000 50000 60000 70000 80000 \
#     --save_iterations 7000 20000 30000 40000 50000 60000 70000 80000 \
#     --distributed_dataset_storage \
#     --distributed_save \
#     --check_cpu_memory