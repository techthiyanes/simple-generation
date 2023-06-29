"""Main module."""
from transformers import (
    AutoModelForCausalLM,
    AutoModelForSeq2SeqLM,
    AutoTokenizer,
    DataCollatorWithPadding,
    GenerationConfig,
    AutoConfig,
)
from tqdm import tqdm
from datasets import Dataset
import torch
from codecarbon import track_emissions


class SimpleGenerator:
    def __init__(
        self,
        model_name_or_path,
        tokenizer_name_or_path=None,
        device_map="auto",
        load_in_8bit=False,
        load_in_4bit=False,
    ):
        config = AutoConfig.from_pretrained(model_name_or_path)
        is_encoder_decoder = getattr(config, "is_encoder_decoder", None)
        if is_encoder_decoder == None:
            print(
                "Could not find 'is_encoder_decoder' in the model config. Assuming it's a seq2seq model."
            )
            is_encoder_decoder = False

        if is_encoder_decoder:
            model_cls = AutoModelForSeq2SeqLM
        else:
            model_cls = AutoModelForCausalLM

        if load_in_4bit and load_in_8bit:
            raise ValueError("Cannot load in both 4bit and 8bit")

        tokenizer_name = (
            tokenizer_name_or_path if tokenizer_name_or_path else model_name_or_path
        )
        self.tokenizer = AutoTokenizer.from_pretrained(
            tokenizer_name, padding_side="left"
        )

        if not getattr(self.tokenizer, "pad_token", None):
            print(
                "Couldn't find a PAD token in the tokenizer, using the EOS token instead."
            )
            self.tokenizer.pad_token = self.tokenizer.eos_token

        model_args = {
            "device_map": device_map,
            "load_in_8bit": load_in_8bit,
            "load_in_4bit": load_in_4bit,
        }

        self.model = model_cls.from_pretrained(model_name_or_path, **model_args).eval()
        self.generation_config = GenerationConfig.from_pretrained(model_name_or_path)

    @track_emissions
    def __call__(
        self,
        texts,
        prefix=None,
        prefix_sep=" ",
        batch_size=2,
        num_workers=4,
        **generation_kwargs,
    ):

        if prefix:
            print("Prefix is set. Adding it to each text.")
            texts = [f"{prefix}{prefix_sep}{text}" for text in texts]

        current_generation_args = self.generation_config.to_dict()

        print("Setting pad_token_id to eos_token_id for open-end generation")
        current_generation_args["pad_token_id"] = self.tokenizer.eos_token_id
        current_generation_args["eos_token_id"] = self.tokenizer.eos_token_id

        print("Using the new 'max_new_tokens' parameter")
        current_generation_args["max_new_tokens"] = current_generation_args.pop(
            "max_length", 20
        )

        if len(generation_kwargs) > 0:
            print(
                "Custom generation args passed. Any named parameters will override the same default one."
            )
            current_generation_args.update(generation_kwargs)

        print("Generation args:", current_generation_args)

        dataset = Dataset.from_dict({"text": texts})
        dataset = dataset.map(
            lambda x: self.tokenizer(x["text"]), batched=True, remove_columns=["text"]
        )

        collator = DataCollatorWithPadding(
            self.tokenizer, pad_to_multiple_of=8, return_tensors="pt"
        )

        loader = torch.utils.data.DataLoader(
            dataset,
            batch_size=batch_size,
            num_workers=num_workers,
            collate_fn=collator,
            pin_memory=True,
        )

        output_texts = list()
        for batch in tqdm(loader, desc="Generation"):
            batch = batch.to(self.model.device)
            output = self.model.generate(
                input_ids=batch["input_ids"],
                attention_mask=batch["attention_mask"],
                **current_generation_args,
            )
            decoded = self.tokenizer.batch_decode(output, skip_special_tokens=True)
            output_texts.extend(decoded)

        return output_texts