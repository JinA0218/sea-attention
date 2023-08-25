#!/usr/bin/env python

import argparse
import os
import subprocess, math
import multiprocessing as mp

SUPPORTED_MODELS = [
    'opt-125m',
    'opt-350m',
    'opt-1.3b',
    'opt-2.7b',
]

SUPPORTED_DATASET = [
    'wikitext2',
]

SUPPORTED_METHODS = [
    'none',
    'perlin',
    'performer',
    'reformer',
]

parser = argparse.ArgumentParser()
parser.add_argument('--model', type=str, required=True, choices=SUPPORTED_MODELS)
parser.add_argument('--method', type=str, required=True, choices=SUPPORTED_METHODS)
parser.add_argument('--dataset', type=str, required=False, default='wikitext2', choices=SUPPORTED_DATASET)
parser.add_argument('--k', type=int, default=64)
parser.add_argument('--predictor-length', type=int, default=256)
parser.add_argument('--nbf', type=int, default=8)

args = parser.parse_args()

if 'CONDA_PREFIX' in os.environ and False:
    conda_prefix = os.environ['CONDA_PREFIX']
    cplus_include_path = os.environ.get('CPLUS_INCLUDE_PATH', '')
    cplus_include_path = f'{conda_prefix}/include/crt:{conda_prefix}/include/thrust:{conda_prefix}/include/cuda:{cplus_include_path}'
    os.environ['CPLUS_INCLUDE_PATH'] = cplus_include_path.strip(':')
    
def get_total_num_gpus():
    try:
        return len(subprocess.check_output(['nvidia-smi', '-L']).decode('utf-8').strip().split('\n'))
    except OSError:
        return 0

n_cpus = mp.cpu_count()
n_gpus = get_total_num_gpus()
n_visible_gpus = len(os.environ.get('CUDA_VISIBLE_DEVICES', ','.join(['0' for i in range(n_gpus)])).strip().split(','))
n_gpus = min(n_gpus, n_visible_gpus)
n_threads_per_proc = int(math.ceil(n_cpus / n_gpus))
os.environ['TORCH_NUM_THREAD'] = str(n_threads_per_proc)

os.environ['PYTHONPATH'] = './'
master_port = os.environ.get('MASTER_PORT', 32042)
deepspeed_config = {
    'opt-125m': { 'wikitext2': { # zero0 by default
        'none': './config/ds_opt_125.json',
        'perlin': './config/ds_opt_125.json',
        'performer': './config/ds_opt_125.json',
        'reformer': './config/ds_opt_125.json',
    }},
    'opt-350m': { 'wikitext2': {
        'none': './config/ds_opt_350.json',
        'perlin': './config/ds_opt_350.json',
        'performer': './config/ds_opt_350_zero2.json',
        'reformer': './config/ds_opt_350_zero2.json',
    }},
    'opt-1.3b': { 'wikitext2': {
        'none': './config/ds_opt_1.3.json',
        'perlin': './config/ds_opt_1.3.json',
        'performer': './config/ds_opt_1.3.json',
        'reformer': './config/ds_opt_1.3.json',
    }},
    'opt-2.7b': { 'wikitext2': {
        'none': './config/ds_opt_2.7.json',
        'perlin': './config/ds_opt_2.7.json',
        'performer': './config/ds_opt_2.7.json',
        'reformer': './config/ds_opt_2.7.json',
    }}
}[args.model][args.dataset][args.method]
# kd_checkpointing = {
#     'opt-125m': { 'wikitext2': {
#         'none': True,
#         'perlin': True,
#         'performer': True,
#         'reformer': True,
#     }},
# }.get(args.model, {'':{'':False}}).get(args.dataset, {'':False}).get(args.method, False)
kd_checkpointing = False

cmd = [
    'deepspeed',
    '--master_port', str(master_port),
    'src/trainer/perlin_trainer.py',
    '--model', args.model,
    '--method', args.method,
    '--dataset', args.dataset,
    '--k', str(args.k),
    '--predictor-length', str(args.predictor_length),
    '--performer-nb-feature-factor', str(args.nbf),
    '--gradient-checkpointing',
    '--deepspeed-enable',
    '--deepspeed',
    '--deepspeed_config', deepspeed_config,
]
if kd_checkpointing:
    cmd.append('--kd-checkpointing')

print('cmd:', ' '.join(cmd))

retcode = subprocess.call(cmd)

print(retcode)
print('[DONE]')
