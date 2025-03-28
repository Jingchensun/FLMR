"""
This script is an example of how to use the pretrained FLMR model for retrieval.
Author: Weizhe Lin
Date: 31/01/2024
For more information, please refer to the official repository of FLMR:
https://github.com/LinWeizheDragon/Retrieval-Augmented-Visual-Question-Answering
"""

import os
from collections import defaultdict

import numpy as np
import torch
from colbert import Indexer, Searcher
from colbert.data import Queries
from colbert.infra import ColBERTConfig, Run, RunConfig
from easydict import EasyDict
from PIL import Image

from transformers import (
    AutoImageProcessor,
    AutoModel,
    AutoTokenizer,
)
from flmr import (
    FLMRModelForRetrieval,
    FLMRQueryEncoderTokenizer,
    FLMRContextEncoderTokenizer,
)
from flmr import index_custom_collection
from flmr import create_searcher, search_custom_collection


def index_corpus(args, custom_collection):
    # Launch indexer
    index_path = index_custom_collection(
        custom_collection=custom_collection,
        model=args.checkpoint_path,
        index_root_path=args.index_root_path,
        index_experiment_name=args.experiment_name,
        index_name=args.index_name,
        nbits=args.nbits, # number of bits in compression
        doc_maxlen=512, # maximum allowed document length
        overwrite=False, # whether to overwrite existing indices
        use_gpu=args.use_gpu, # whether to enable GPU indexing
        indexing_batch_size=args.indexing_batch_size,
        model_temp_folder="tmp",
        nranks=1, # number of GPUs used in indexing
    )
    return index_path


def query_index(args, ds, passage_contents, flmr_model: FLMRModelForRetrieval):
    # Search documents
    # initiate a searcher
    searcher = create_searcher(
        index_root_path=args.index_root_path,
        index_experiment_name=args.experiment_name,
        index_name=args.index_name,
        nbits=args.nbits, # number of bits in compression
        use_gpu=args.use_gpu, # whether to enable GPU searching
    )

    def encode_and_search_batch(batch, Ks):
        # encode queries
        input_ids = torch.LongTensor(batch["input_ids"]).to("cuda")
        # print(query_tokenizer.batch_decode(input_ids, skip_special_tokens=False))
        attention_mask = torch.LongTensor(batch["attention_mask"]).to("cuda")
        pixel_values = torch.FloatTensor(batch["pixel_values"]).to("cuda")
        query_input = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "pixel_values": pixel_values,
        }
        query_embeddings = flmr_model.query(**query_input).late_interaction_output
        query_embeddings = query_embeddings.detach().cpu()

        # search
        custom_quries = {
            question_id: question for question_id, question in zip(batch["question_id"], batch["question"])
        }
        ranking = search_custom_collection(
            searcher=searcher,
            queries=custom_quries,
            query_embeddings=query_embeddings,
            num_document_to_retrieve=max(Ks), # how many documents to retrieve for each query
            centroid_search_batch_size=args.centroid_search_batch_size,
        )

        ranking_dict = ranking.todict()

        # Process ranking data and obtain recall scores
        recall_dict = defaultdict(list)
        for question_id, answers in zip(batch["question_id"], batch["answers"]):
            retrieved_docs = ranking_dict[question_id]
            retrieved_docs = [doc[0] for doc in retrieved_docs]
            retrieved_doc_texts = [passage_contents[doc_idx] for doc_idx in retrieved_docs]
            hit_list = []
            for retrieved_doc_text in retrieved_doc_texts:
                found = False
                for answer in answers:
                    if answer.strip().lower() in retrieved_doc_text.lower():
                        found = True
                if found:
                    hit_list.append(1)
                else:
                    hit_list.append(0)

            # print(hit_list)
            # input()
            for K in Ks:
                recall = float(np.max(np.array(hit_list[:K])))
                recall_dict[f"Recall@{K}"].append(recall)

        batch.update(recall_dict)
        return batch

    flmr_model = flmr_model.to("cuda")
    print("Starting encoding...")
    Ks = args.Ks
    # ds = ds.select(range(2000, 2100))
    ds = ds.map(
        encode_and_search_batch,
        fn_kwargs={"Ks": Ks},
        batched=True,
        batch_size=args.query_batch_size,
        load_from_cache_file=False,
        new_fingerprint="avoid_cache",
    )

    return ds


def main(args):
    from datasets import load_dataset

    ds = load_dataset(args.dataset_path)
    passage_ds = load_dataset(args.passage_dataset_path)

    print("========= Loading dataset =========")
    print(ds)
    print(passage_ds)

    def add_path_prefix_in_img_path(example, prefix):
        example["img_path"] = os.path.join(prefix, example["img_path"])
        new_ROIs = []
        for ROI in example["ROIs"]:
            ROI = os.path.join(prefix, ROI)
            new_ROIs.append(ROI)
        example["ROIs"] = new_ROIs
        return example

    ds = ds.map(add_path_prefix_in_img_path, fn_kwargs={"prefix": args.image_root_dir})

    use_split = args.use_split

    ds = ds[use_split]
    passage_ds = passage_ds[f"{use_split}_passages"]
    print("========= Data Summary =========")
    print("Number of examples:", len(ds))
    print("Number of passages:", len(passage_ds))

    print("========= Indexing =========")
    # Run indexing on passages
    passage_contents = passage_ds["passage_content"]
    passage_contents = ["<BOK> " + passage + " <EOK>" for passage in passage_contents]
    if args.run_indexing:
        ## Call ColBERT indexing to index passages
        index_corpus(args, passage_contents)
    else:
        print("args.run_indexing is False, skipping indexing...")

    print("========= Loading pretrained model =========")
    # 加载 text_config
    from flmr.models.flmr.configuration_flmr import FLMRTextConfig
    
    text_config = FLMRTextConfig.from_pretrained(os.path.join(args.checkpoint_path, "query_tokenizer"))

    # 传 text_config 进去
    query_tokenizer = FLMRQueryEncoderTokenizer.from_pretrained(
        args.checkpoint_path, subfolder="query_tokenizer", text_config=text_config
    )
    # query_tokenizer = FLMRQueryEncoderTokenizer.from_pretrained(args.checkpoint_path, subfolder="query_tokenizer")
    context_tokenizer = FLMRContextEncoderTokenizer.from_pretrained(
        args.checkpoint_path, subfolder="context_tokenizer"
    )

    flmr_model = FLMRModelForRetrieval.from_pretrained(
        args.checkpoint_path,
        query_tokenizer=query_tokenizer,
        context_tokenizer=context_tokenizer,
    )
    image_processor = AutoImageProcessor.from_pretrained(args.image_processor_name)

    print("========= Preparing query input =========")

    def prepare_inputs(sample, num_ROIs=9):
        sample = EasyDict(sample)

        module = EasyDict(
            {"type": "QuestionInput", "option": "default", "separation_tokens": {"start": "<BOQ>", "end": "<EOQ>"}}
        )
        text_sequence = " ".join([module.separation_tokens.start] + [sample.question] + [module.separation_tokens.end])

        module = EasyDict(
            {
                "type": "TextBasedVisionInput",
                "option": "object",
                "object_max": 40,
                "attribute_max": 3,
                "attribute_thres": 0.05,
                "ocr": 1,
                "separation_tokens": {"start": "<BOV>", "sep": "<SOV>", "end": "<EOV>"},
            }
        )

        vision_sentences = []
        vision_sentences += [module.separation_tokens.start]
        for obj in sample.objects:
            attribute_max = module.get("attribute_max", 0)
            if attribute_max > 0:
                # find suitable attributes
                suitable_attributes = []
                for attribute, att_score in zip(obj["attributes"], obj["attribute_scores"]):
                    if att_score > module.attribute_thres and len(suitable_attributes) < attribute_max:
                        suitable_attributes.append(attribute)
                # append to the sentence
                vision_sentences += suitable_attributes
            vision_sentences.append(obj["class"])
            vision_sentences.append(module.separation_tokens.sep)

        ocr = module.get("ocr", 0)
        if ocr > 0:
            text_annotations = sample.img_ocr
            filtered_descriptions = []
            for text_annoation in text_annotations:
                description = text_annoation["description"].strip()
                description = description.replace("\n", " ")  # remove line switching
                # vision_sentences += [description]
                # print('OCR feature:', description)
                if description not in filtered_descriptions:
                    filtered_descriptions.append(description)
            # print('OCR feature:', filtered_descriptions)
            vision_sentences += filtered_descriptions

        vision_sentences += [module.separation_tokens.end]
        text_sequence = text_sequence + " " + " ".join(vision_sentences)

        module = EasyDict(
            {
                "type": "TextBasedVisionInput",
                "option": "caption",
                "separation_tokens": {"start": "<BOC>", "end": "<EOC>"},
            }
        )
        if isinstance(sample.img_caption, dict):
            caption = sample.img_caption["caption"]
        else:
            caption = sample.img_caption
        text_sequence = (
            text_sequence
            + " "
            + " ".join([module.separation_tokens.start] + [caption] + [module.separation_tokens.end])
        )

        sample["text_sequence"] = text_sequence

        # Take the first num_ROIs if there are more than num_ROIs ROIs
        if num_ROIs > len(sample["ROIs"]):
            sample["ROIs"] = sample["ROIs"] + [sample["ROIs"][-1]] * (num_ROIs - len(sample["ROIs"]))
        sample["ROIs"] = sample["ROIs"][:num_ROIs]

        return sample

    # Prepare inputs using the same configuration as in the original FLMR paper
    ds = ds.map(prepare_inputs)

    def tokenize_inputs(examples, query_tokenizer, image_processor):
        encoding = query_tokenizer(examples["text_sequence"])
        examples["input_ids"] = encoding["input_ids"]
        examples["attention_mask"] = encoding["attention_mask"]

        pixel_values = []
        for img_path, ROIs in zip(examples["img_path"], examples["ROIs"]):
            image = Image.open(img_path).convert("RGB")
            all_images = [image]
            for ROI in ROIs:
                # parse the ROI. The ROI is formatted as {img_path}|||{class}_{xmin}_{ymin}_{xmax}_{ymax}
                img_path, remaining = ROI.split("|||")
                img = Image.open(img_path).convert("RGB")
                class_name, xmin, ymin, xmax, ymax = remaining.split("_")
                xmin, ymin, xmax, ymax = float(xmin), float(ymin), float(xmax), float(ymax)
                crop = (xmin, ymin, xmax, ymax)
                # if the size of the crop is too small, enlarge the crop to at least 5 pixels in size
                if xmax - xmin < 5 and ymax - ymin < 5:
                    if xmax - xmin < 5:
                        xmin = max(0, xmin - 2.5)
                        xmax = min(img.size[0], xmax + 2.5)
                    if ymax - ymin < 5:
                        ymin = max(0, ymin - 2.5)
                        ymax = min(img.size[1], ymax + 2.5)
                    print("enlarged: ", crop, (xmin, ymin, xmax, ymax))
                    crop = (xmin, ymin, xmax, ymax)

                all_images.append(img.crop(crop))

            encoded = image_processor(all_images, return_tensors="pt")
            pixel_values.append(encoded.pixel_values)

        pixel_values = torch.stack(pixel_values, dim=0)
        examples["pixel_values"] = pixel_values
        return examples

    # Tokenize and prepare image pixels for input
    ds = ds.map(
        tokenize_inputs,
        fn_kwargs={"query_tokenizer": query_tokenizer, "image_processor": image_processor},
        batched=True,
        batch_size=8,
        num_proc=16,
    )

    print("========= Querying =========")
    ds = query_index(args, ds, passage_contents, flmr_model)
    # Compute final recall
    print("=============================")
    print("Inference summary:")
    print("=============================")
    print(f"Total number of questions: {len(ds)}")

    for K in args.Ks:
        recall = np.mean(np.array(ds[f"Recall@{K}"]))
        print(f"Recall@{K}:\t", recall)

    print("=============================")
    print("Done! Program exiting...")


if __name__ == "__main__":
    # Initialize arg parser
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--use_gpu", action="store_true")
    # all hardcode parameters should be here
    parser.add_argument("--query_batch_size", type=int, default=8)
    parser.add_argument("--num_ROIs", type=int, default=9)
    parser.add_argument("--dataset_path", type=str, default="OKVQA_FLMR_prepared_data.hf")
    parser.add_argument(
        "--passage_dataset_path", type=str, default="OKVQA_FLMR_prepared_passages_with_GoogleSearch_corpus.hf"
    )
    parser.add_argument("--image_root_dir", type=str, default="./ok-vqa/")
    parser.add_argument("--use_split", type=str, default="test")
    parser.add_argument("--index_root_path", type=str, default=".")
    parser.add_argument("--index_name", type=str, default="OKVQA_GS")
    parser.add_argument("--experiment_name", type=str, default="OKVQA_GS")
    parser.add_argument("--indexing_batch_size", type=int, default=64)
    parser.add_argument("--image_processor_name", type=str, default="openai/clip-vit-base-patch32")
    parser.add_argument("--nbits", type=int, default=8)
    parser.add_argument("--Ks", type=int, nargs="+", default=[5, 10, 20, 50, 100])
    parser.add_argument("--checkpoint_path", type=str, default="./converted_flmr")
    parser.add_argument("--run_indexing", action="store_true")
    parser.add_argument("--centroid_search_batch_size", type=int, default=None)

    args = parser.parse_args()
    """
    Example usage:
    python example_use_flmr.py \
            --use_gpu --run_indexing \
            --index_root_path "." \
            --index_name OKVQA_GS\
            --experiment_name OKVQA_GS \
            --indexing_batch_size 64 \
            --image_root_dir /path/to/KBVQA_data/ok-vqa/ \
            --dataset_path LinWeizheDragon/OKVQA_FLMR_preprocessed_data \
            --passage_dataset_path LinWeizheDragon/OKVQA_FLMR_preprocessed_GoogleSearch_passages \
            --use_split test \
            --nbits 8 \
            --Ks 1 5 10 20 50 100 \
            --checkpoint_path LinWeizheDragon/FLMR \
            --image_processor_name openai/clip-vit-base-patch32 \
            --query_batch_size 8 \
            --num_ROIs 9 \
    """
    main(args)
