# coding: utf-8
import os
os.environ['TOKENIZERS_PARALLELISM'] = 'False'
import warnings
import gc
import time
from typing import Dict, Any

import numpy as np
import torch
import transformers
from transformers import AutoModel, AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig, GenerationConfig
from transformers import StoppingCriteria, StoppingCriteriaList
from peft import PeftModel, LoraConfig, get_peft_model, prepare_model_for_kbit_training

import spacy
from ltp import LTP
import langdetect
langdetect.DetectorFactory.seed = 0
from utils import get_ents_en, get_ents_zh, add_pinyin, get_labelled_text

import json
from tqdm import tqdm

import openai
# openai.api_base = "https://cp.ojkb.xyz/v1"
openai.api_key = "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"

# specify tasks
# tasks = ['abs', 'poli', 'trans']
tasks = ['trans']

# specify base model
base_model = 'bloomz-560m'
# base_model = 'bloomz-1b7'
base_model_dir = f'./models/{base_model}'

# specify langauge
lang = 'en'

# specify lora weights
seek_model_path = f"./lora_weights/seek-%s_label_{base_model}_{lang}/checkpoint-2700"

# special tokens
DEFAULT_PAD_TOKEN = '[PAD]'
DEFAULT_EOS_TOKEN = '</s>'
DEFAULT_BOS_TOKEN = '<s>'
DEFAULT_UNK_TOKEN = '<unk>'

bnb_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_use_double_quant=True,
    bnb_4bit_quant_type='nf4',
    bnb_4bit_compute_dtype=torch.bfloat16,
)

def smart_tokenizer_and_embedding_resize(
    tokenizer: transformers.PreTrainedTokenizer,
    model: transformers.PreTrainedModel,
):
    """Resize tokenizer and embedding.

    Note: This is the unoptimized version that may make your embedding size not be divisible by 64.
    """
    special_tokens_dict: Dict[str, str] = {}
    if tokenizer.pad_token is None:
        special_tokens_dict['pad_token'] = DEFAULT_PAD_TOKEN
    if tokenizer.eos_token is None:
        special_tokens_dict['eos_token'] = DEFAULT_EOS_TOKEN
    if tokenizer.bos_token is None:
        special_tokens_dict['bos_token'] = DEFAULT_BOS_TOKEN
    if tokenizer.unk_token is None:
        special_tokens_dict['unk_token'] = DEFAULT_UNK_TOKEN
    num_new_tokens = tokenizer.add_special_tokens(special_tokens_dict)
    model.resize_token_embeddings(len(tokenizer))

    if num_new_tokens > 0:
        input_embeddings = model.get_input_embeddings().weight.data
        output_embeddings = model.get_output_embeddings().weight.data

        input_embeddings_avg = input_embeddings[:-num_new_tokens].mean(
            dim=0, keepdim=True)
        output_embeddings_avg = output_embeddings[:-num_new_tokens].mean(
            dim=0, keepdim=True)

        input_embeddings[-num_new_tokens:] = input_embeddings_avg
        output_embeddings[-num_new_tokens:] = output_embeddings_avg

def hide_text(raw_input, spacy_model):
    return get_labelled_text(raw_input, spacy_model, return_ents=False)

def get_api_output(subed_text, task_type, lang):
    with open(f'./prompts/v5/api_{task_type}_label_{lang}.txt', 'r', encoding='utf-8') as f:
        template = f.read()
    response = openai.ChatCompletion.create(
            #   model="gpt-4",
              model="gpt-3.5-turbo",
              temperature=0.1,
              messages=[
                    {"role": "user", "content": template % subed_text}
                ]
            )
    return response['choices'][0]['message']['content'].strip(" \n")

def recover_text(sub_content, sub_output, content, model, tokenizer, task_type, lang):
    re_model = PeftModel.from_pretrained(model, seek_model_path % task_type, quantization_config=bnb_config, device_map='cuda:0', trust_remote_code=True)
    with open(f'./prompts/v5/seek_{task_type}_{lang}.txt', 'r', encoding='utf-8') as f:
        initial_prompt = f.read()
    input_text = initial_prompt % (sub_content, sub_output, content)
    input_text += tokenizer.bos_token
    inputs = tokenizer(input_text, return_tensors='pt')
    inputs = inputs.to('cuda:0')
    len_prompt = len(inputs['input_ids'][0])
    def custom_stopping_criteria(input_ids: torch.LongTensor, score: torch.FloatTensor, **kwargs) -> bool:
        cur_top1 = tokenizer.decode(input_ids[0,len_prompt:])
        if '\n' in cur_top1 or tokenizer.eos_token in cur_top1:
            return True
        return False
    pred = re_model.generate(
        **inputs, 
        generation_config = GenerationConfig(
            max_new_tokens=1024,
            do_sample=False,
            num_beams=3,
            ),
        stopping_criteria = StoppingCriteriaList([custom_stopping_criteria])
        )
    pred = pred.cpu()[0][len(inputs['input_ids'][0]):]
    recovered_text = tokenizer.decode(pred, skip_special_tokens=True).split('\n')[0]
    torch.cuda.empty_cache()
    gc.collect()
    return recovered_text

if __name__ == '__main__':
    # load models
    print('loading model...')
    model = AutoModelForCausalLM.from_pretrained(base_model_dir, quantization_config=bnb_config, device_map='cuda:0', trust_remote_code=True, torch_dtype=torch.float16)
    tokenizer = AutoTokenizer.from_pretrained(base_model_dir, trust_remote_code=True)
    smart_tokenizer_and_embedding_resize(tokenizer=tokenizer,model=model)
    spacy_model = spacy.load(f'{lang}_core_web_trf')
    # ltp = LTP("LTP/small")
    # if torch.cuda.is_available():
    #     ltp.cuda()

    DATA_DIR = "/home/ykwy/EnochPB/USPB/qTest"
    OUTPUT_DIR = "./output-HaS-label"
    dir_list = os.listdir(DATA_DIR)
    docs = []
    len_list = []
    print('hiding text...')
    for dir_name in tqdm(dir_list):
        data_file = os.path.join(DATA_DIR, dir_name, 'longResult.json')
        out_file = os.path.join(OUTPUT_DIR, dir_name + ".txt")
        with open(data_file, 'r') as rf:
            data = json.load(rf)
            queries = eval(data['gptAnswerInList'])
        hidden_text = []
        for query in queries:
            hidden_text.append(hide_text(query, spacy_model))
        with open(out_file, 'w') as wf:
            json.dump(hidden_text, wf)

    # while True:
    #     # input text
    #     raw_input = input('\033[1;31minput:\033[0m ')
    #     if raw_input == 'q':
    #         print('quit')
    #         break
    #     # hide
    #     hidden_text_list = hide_text(raw_input, spacy_model)
    #     print('\033[1;31mhidden text:\033[0m ', hidden_text_list)
    #     # seek
    #     for task_type in tasks:
    #         sub_output = get_api_output(hidden_text_list, task_type, lang).replace('\n', ';')
    #         print(f'\033[1;31mhidden output for {task_type}:\033[0m ', sub_output)
    #         if lang == 'zh' and task_type == 'translate':
    #             raw_input = add_pinyin(raw_input, ltp)
    #         output_text = recover_text(hidden_text_list, sub_output, raw_input, model, tokenizer, task_type, lang)
    #         print(f'\033[1;31mrecovered output for {task_type}:\033[0m ', output_text)
