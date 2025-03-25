python example_use_flmr.py \
            --use_gpu --run_indexing \
            --index_root_path "." \
            --index_name OKVQA_GS\
            --experiment_name OKVQA_GS \
            --indexing_batch_size 64 \
            --image_root_dir /path/to/KBVQA_data/ok-vqa/ \
            --dataset_path BByrneLab/OKVQA_FLMR_preprocessed_data \
            --passage_dataset_path BByrneLab/OKVQA_FLMR_preprocessed_GoogleSearch_passages \
            --use_split test \
            --nbits 8 \
            --Ks 1 5 10 20 50 100 \
            --checkpoint_path LinWeizheDragon/FLMR \
            --image_processor_name openai/clip-vit-base-patch32 \
            --query_batch_size 8 \
            --num_ROIs 9 \