import math
import warnings
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import torch
import torch.nn.functional as F
from performer_pytorch import FastAttention
from torch import nn, optim
from xformers.components.attention.core import (
    SparseCS,
    scaled_dot_product_attention
)

from ...utils import get_bench
from ..common.kl_div_for_atten import kl_div_attention
from ..common.lora import (
    LoraLinear, 
    lora_forward, 
    lora_forward_linear,
    lora_forward_lora
)
from ..common.performer import ProjectionUpdater
from ..hf_bert import BertConfig
from .config import PerlinAttentionConfig, get_default_config
from ...utils import raise_if_nan
# NOTE HJ comment below to debug NaN
raise_if_nan = lambda x: x

timer = lambda name: get_bench().region(name)

# NOTE HJ for temperaty development
T_MASK = None

def interpolate(x: torch.Tensor, size, interp_mode: str = None):
    interp_mode = ('bilinear' if size[-1] >= x.shape[-1] else 'area') if interp_mode is None else interp_mode
    
    if torch.get_autocast_gpu_dtype() == torch.bfloat16: # F interpolate is not supported on bf16
        original_dtype = x.dtype
        with torch.autocast('cuda', torch.float16):
            if x.dtype != torch.float16:
                x = x.to(torch.float16)
            x = F.interpolate(x, size, mode=interp_mode)
        if x.dtype != original_dtype:
            x = x.to(original_dtype)
    else:
        x = F.interpolate(x, size, mode=interp_mode)
    
    return x

@dataclass
class PerlinAttentionOutput:
    loss: torch.Tensor
    context_layer: torch.Tensor
    partial_attention_probs: torch.Tensor
    estimated_attention_probs: torch.Tensor
    dense_attention_probs: torch.Tensor
    key_for_score: torch.Tensor

class PerlinAttention(nn.Module):
    def __init__(
        self,
        config: BertConfig,
        perlin_config: PerlinAttentionConfig = None,
    ):
        super().__init__()
    
        self.config = config
        self.pconfig = perlin_config if perlin_config is not None else get_default_config()
        
        self.num_attention_heads = config.num_attention_heads
        self.attention_head_size = int(config.hidden_size / config.num_attention_heads)
        self.all_head_size = self.num_attention_heads * self.attention_head_size
        
        ### Perlin
        #- configs
        self.benchmarking = False
        
        #- attention predictor
        #-- mlp predictor
        self.performer_nb_features = int(
            self.attention_head_size * math.log(self.attention_head_size) / self.pconfig.performer_nb_factor
        )
        self.performer = FastAttention(
            dim_heads = self.attention_head_size,
            nb_features = self.performer_nb_features,
            causal=False, # NOTE HJ if we handle causal attention, this should be changed.
        )
        self.performer_proj_updater = ProjectionUpdater(
            self.performer, 
            1000,
        )
        performer_value_hidden_size = self.attention_head_size*3
        self.attention_predictor_enc = nn.Sequential(
            nn.Dropout(0.1),
            nn.Linear(performer_value_hidden_size, self.attention_head_size*2),
            nn.LayerNorm(self.attention_head_size*2),
            nn.GELU(),
        )
        self.attention_predictor_dec_row = nn.Sequential(
            nn.Linear(self.attention_head_size*2, self.pconfig.attention_predictor_length),
        )
        self.attention_predictor_cnn = nn.Sequential(
            nn.Conv2d(12, 12, 3, padding=1),
            nn.GELU(),
            nn.Conv2d(12, 12, 3, padding=1),
            nn.GELU(),
            nn.Conv2d(12, 12, 3, padding=1),
            nn.GELU(),
            nn.Conv2d(12, 12, 3, padding=1),
            nn.GELU(),
            nn.Conv2d(12, 12, 3, padding=1),
            nn.GELU(),
        )
        self.attention_predictor_dec_scaler = nn.Sequential(
            nn.Linear(self.attention_head_size*2, 2),
        )
        
        #-- compressed predictor
        self.attention_predictor_comp_length = \
            self.pconfig.attention_predictor_comp_patch_count * self.pconfig.attention_predictor_comp_patch_size
        self.attention_predictor_comp_codebook = nn.Parameter(
            torch.randn((self.pconfig.attention_predictor_comp_book_size, self.pconfig.attention_predictor_comp_patch_size))
        )
        self.attention_predictor_comp_enc = nn.Sequential(
            nn.Dropout(0.1),
            nn.Linear(performer_value_hidden_size, self.attention_head_size*2),
            nn.LayerNorm(self.attention_head_size*2),
            nn.GELU(),
        )
        self.attention_predictor_comp_dec_row = nn.Sequential(
            nn.Linear(
                self.attention_head_size*2,
                self.pconfig.attention_predictor_comp_book_size * self.pconfig.attention_predictor_comp_patch_count
            ),
        )
        #-- TODO VQVAE
        
        #- output
        # NOTE out linear is removed, following section is just for in case we revert this change...
        # self.out = nn.Sequential(
        #     nn.Dropout(0.1),
        #     nn.Linear(self.all_head_size*2, config.hidden_size),
        #     nn.LayerNorm(config.hidden_size),
        #     nn.GELU(),
        #     nn.Linear(config.hidden_size, config.hidden_size),
        # )
        # self.out_random_lookup = nn.Sequential(
        #     nn.Dropout(0.1),
        #     nn.Linear(self.all_head_size*3, config.hidden_size),
        #     nn.LayerNorm(config.hidden_size),
        #     nn.GELU(),
        #     nn.Linear(config.hidden_size, config.hidden_size),
        # )
        
        self.norm_performer = nn.LayerNorm(config.hidden_size)
        self.norm_partial = nn.LayerNorm(config.hidden_size)
        self.norm_random = nn.LayerNorm(config.hidden_size)
        self.norm = nn.LayerNorm(config.hidden_size)
        
        self.register_buffer('_v_eye', None)
        self._v_eye = torch.eye(
            self.pconfig.v_eye_length, dtype=torch.float32
        ).view(1, 1, self.pconfig.v_eye_length, self.pconfig.v_eye_length)
    
    def forward(
        self,
        q: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
        q_for_atten: torch.Tensor,
        k_for_atten: torch.Tensor,
        v_for_atten: torch.Tensor,
        q_for_score: torch.Tensor,
        k_for_score: torch.Tensor,
        attention_mask: torch.Tensor,
        attention_scores_truth: torch.Tensor,
        context_layer_truth: torch.Tensor,
    ):
        if q.dtype in [torch.float16, torch.bfloat16]:
            # NOTE HJ even if we are in bfloat16, we have to use fp16 minimum because of F.interpolate
            FP_MIN = torch.finfo(torch.float16).min / 2
        elif q.dtype in [torch.float32]:
            FP_MIN = torch.finfo(torch.float32).min / 2
        else:
            raise Exception('unknown type')
        
        raise_if_nan(q)
        raise_if_nan(k)
        raise_if_nan(v)
        
        with timer("perlin"):
            N, H, T, HID = q.shape
            with timer("vmask"):
                v_for_atten_identity = interpolate(
                    x=self._v_eye,
                    size=v_for_atten.shape[-2:],
                    interp_mode='nearest'
                ).expand(v_for_atten.shape).contiguous()
                
                v_for_atten = torch.cat([
                    v_for_atten_identity, 
                    v_for_atten
                ], dim=-1)
                v_for_atten.masked_fill_(attention_mask.transpose(-1, -2) < -1, 0)
            
            with timer("performer"):
                if not self.benchmarking:
                    q_type = q_for_atten.dtype
                    with torch.autocast('cuda', torch.float32):
                        performer_context_layer = self.performer(
                            q_for_atten, 
                            k_for_atten, 
                            v_for_atten
                        )
                    if q_type != performer_context_layer.dtype:
                        performer_context_layer = performer_context_layer.to(q_type)
                else:
                    # TODO: fix numerical stability...
                    performer_context_layer = self.performer(
                        q_for_atten, 
                        k_for_atten, 
                        v_for_atten
                    )
            
            with timer("performer_value"):
                # NOTE HJ Cut gradient from loss_sp, because loss_sp has negative effect to loss_model when approximation is sucks.
                performer_value = torch.cat([
                    performer_context_layer, 
                    v
                ], dim=-1).detach()
            
            # estimate attention scores
            with timer("predictor"):
                if self.pconfig.attention_predictor_method == 'mlp':
                    t_attention_predictor = self.attention_predictor_enc(performer_value)
                    estimated_attention_score = self.attention_predictor_dec_row(t_attention_predictor) # type: torch.Tensor
                    estimated_attention_score = self.attention_predictor_cnn(estimated_attention_score)
                elif self.pconfig.attention_predictor_method == 'comp':
                    warnings.warn('attention prediction method is compressed one.')
                    t_attention_predictor = self.attention_predictor_comp_enc(performer_value)
                    estimated_attention_score = self.attention_predictor_comp_dec_row(t_attention_predictor)
                    estimated_attention_score = estimated_attention_score\
                        .view(N, H, T, self.pconfig.attention_predictor_comp_patch_count, self.pconfig.attention_predictor_comp_book_size)
                    _, _, _, CODE_SEQ_LEN, BOOK_LEN = estimated_attention_score.shape
                    estimated_attention_score = torch.softmax(estimated_attention_score, dim = -1)
                    estimated_attention_score = torch.matmul(
                        estimated_attention_score.view(-1, BOOK_LEN), 
                        self.attention_predictor_comp_codebook
                    )
                    estimated_attention_score = estimated_attention_score.view(N, H, T, -1)
                else:
                    raise Exception()
            
            # interpolate and convert to probability
            with timer("mask_softmax"):
                resized_attention_mask = interpolate(
                    x=attention_mask, 
                    size=(1, estimated_attention_score.shape[-1]), 
                    interp_mode='nearest',
                )
                resized_attention_mask_binary = resized_attention_mask < -1
                # resized_attention_mask = (resized_attention_mask < -1) * FP_MIN
                if not self.benchmarking:
                    estimated_attention_score_unmasked = estimated_attention_score
                    estimated_attention_score = estimated_attention_score.masked_fill(
                        mask=resized_attention_mask_binary,
                        value=FP_MIN
                    )
                else:
                    estimated_attention_score = estimated_attention_score.masked_fill_(
                        mask=resized_attention_mask_binary,
                        value=FP_MIN
                    )
                estimated_attention_probs = torch.softmax(estimated_attention_score, -1)
            
            # in layerwise, train perlin attention predictor
            loss = 0
            if not self.benchmarking:
                # for loss calculation
                estimated_attention_probs_resized = interpolate(
                    x=estimated_attention_probs, 
                    size=(T, T), 
                    interp_mode='nearest'
                )
                estimated_attention_score_resized = interpolate(
                    x=estimated_attention_score_unmasked, 
                    size=(T, T), 
                    interp_mode='nearest'
                )
                
                with torch.autocast('cuda', torch.float32):
                    loss_kl = kl_div_attention(
                        F.log_softmax(estimated_attention_score_resized + attention_mask, dim=-1),
                        F.softmax(attention_scores_truth + attention_mask, dim=-1),
                        attention_mask,
                    ) * 0.1
                    raise_if_nan(loss_kl)
                    loss_mse = F.mse_loss(
                        torch.softmax(estimated_attention_score_resized + attention_mask, dim=-1), 
                        torch.softmax(attention_scores_truth + attention_mask, dim=-1)
                    )
                    raise_if_nan(loss_mse)
                    loss += loss_kl + loss_mse
                    raise_if_nan(loss)
            
            with timer("mask"):
                T_M = estimated_attention_probs.shape[-1]
                top_k = min(max(int(round(self.pconfig.k * (T_M / T))), 1), T_M)
                k_flatten = self.pconfig.k_flatten
                if not k_flatten:
                    with timer("mask.topk"):
                        _, indices = torch.topk(
                            estimated_attention_probs, # estimation gradient is cut here
                            k=top_k, 
                            dim=-1, 
                            sorted=True,
                        )
                    with timer("mask.empty"):
                        partial_attention_mask = torch.empty(
                            (N, H, T, T_M),
                            dtype=q_for_score.dtype,
                            device=q_for_score.device,
                        )
                    with timer("mask.fill"):
                        partial_attention_mask.fill_(FP_MIN)
                    with timer("mask.scatter"):
                        partial_attention_mask.scatter_(dim=-1, index=indices, value=0)
                else:
                    k_flatten_dim = self.pconfig.k_flatten_dim
                    assert k_flatten_dim in ['head', 'batch']
                    with timer("mask.view"):
                        t = (estimated_attention_probs * (attention_mask.transpose(-1, -2) > -1)).view(N, H*T*T_M)
                    with timer("mask.topk"):
                        _, indices = torch.topk(
                            input=t,
                            k=top_k*T*H if k_flatten_dim == 'batch' else top_k*T, 
                            dim=-1, 
                            sorted=True #sorted true is important
                        )
                    with timer("mask.empty"):
                        partial_attention_mask = torch.empty(
                            t.shape, 
                            dtype=torch.long, 
                            device=attention_mask.device,
                        )
                    with timer("mask.fill"):
                        partial_attention_mask.fill_(t.shape[-1])
                    with timer("mask.scatter"):
                        partial_attention_mask.scatter_(
                            dim=-1,
                            index=indices,
                            src=torch.arange(
                                top_k*T*H if k_flatten_dim == 'batch' else top_k*T, 
                                dtype=torch.long,
                                device=attention_mask.device, 
                            )\
                                .view((1, -1) if k_flatten_dim == 'batch' else (1, 1, -1))\
                                .expand(indices.shape)
                        )
                    with timer("mask.masked_fill"):
                        token_length = (attention_mask > -1).long().sum(-1).view(N, -1)
                        t_dead_mask = partial_attention_mask >= (token_length * (top_k * H if k_flatten_dim == 'batch' else top_k)) #k is resized
                        # partial_attention_mask.fill_(FP_MIN)
                        # partial_attention_mask.masked_fill_(t_alive_mask, value=0)
                        partial_attention_mask = t_dead_mask.to(q.dtype) * FP_MIN
                    partial_attention_mask = partial_attention_mask.view(N, H, T, T_M)
            
            with timer("interp"):
                # NOTE: partial attention mask should be filled with 0 and -inf only.
                raise_if_nan(partial_attention_mask)
                partial_attention_mask = interpolate(
                    x=partial_attention_mask, 
                    size=(T, T), 
                    interp_mode='nearest'
                )
                raise_if_nan(partial_attention_mask)
            
            with timer("attention"):
                if not self.benchmarking:
                    # start of masked attention mechanism
                    
                    # NOTE: checking avearge k is expected. uncomment following print, and then run visualize_glue
                    # print(
                    #     (partial_attention_mask > -1).long().sum(-1).sum(-1)[:,0].view(-1),
                    #     (attention_mask > -1).long().sum(-1).view(-1),
                    #     (partial_attention_mask > -1).long().sum(-1).sum(-1)[:,0].view(-1) / (attention_mask > -1).long().sum(-1).view(-1),
                    #     k, T, T_M
                    # )
                    # NOTE: print avg k per batch
                    # print(((partial_attention_mask > -1).view(N, -1).long().sum(-1) / (attention_mask > -1).long().view(N, -1).sum(-1)).mean() / H)
                    
                    attention_scores_dense = torch.matmul(q_for_score, k_for_score.transpose(-1, -2))
                    attention_scores_dense = attention_scores_dense / math.sqrt(self.attention_head_size)
                    loss += F.mse_loss(
                        attention_scores_dense.masked_fill(attention_mask < -1, 0), 
                        attention_scores_truth.masked_fill(attention_mask < -1, 0),
                    ) * 0.5
                    raise_if_nan(loss)
                    
                    # NOTE HJ `attention_probs_dense` is for visualization, therefore it will not computed on benchmarking mode
                    if attention_mask is not None:
                        attention_scores_dense_masked = attention_scores_dense + attention_mask
                    attention_probs_dense = torch.softmax(attention_scores_dense_masked, dim=-1)
                    
                    # NOTE HJ you should not add attention_mask and attention_score, because partial_attention_mask already has it.
                    # print(
                    #     torch.unique((partial_attention_mask).view(-1)), 
                    #     torch.max(attention_scores_dense.view(-1)), 
                    #     torch.min(attention_scores_dense.view(-1)),
                    #     attention_scores_dense.dtype, partial_attention_mask.dtype
                    # )
                    raise_if_nan(partial_attention_mask)
                    partial_attention_scores = attention_scores_dense + partial_attention_mask
                    raise_if_nan(partial_attention_scores)
                    partial_attention_probs = torch.softmax(partial_attention_scores, -1)
                    partial_attention_probs = partial_attention_probs * (partial_attention_mask > -1)
                    raise_if_nan(partial_attention_probs)
                    
                    # perform scaling, however this pervent to use spase attention kernel
                    estimated_scales = self.attention_predictor_dec_scaler(t_attention_predictor)
                    if self.pconfig.partial_attention_scaler:
                        partial_attention_probs = partial_attention_probs * torch.sigmoid(estimated_scales[..., 0:1])
                    
                    raise_if_nan(partial_attention_probs)
                    raise_if_nan(v)
                    partial_context_layer = torch.matmul(partial_attention_probs, v)
                    
                    
                    average_context_layer = (
                        v *\
                        (attention_mask.transpose(-1, -2) > -1) *\
                        interpolate(estimated_attention_probs.mean(-2, keepdim=True), (1, T)).transpose(-1, -2)
                    ).sum(-2, keepdim=True)
                    average_scale = torch.sigmoid(estimated_scales[..., 1:2])
                    partial_context_layer = partial_context_layer * average_scale + (1-average_scale) * average_context_layer
                    
                    # average_context_layer = (v * (attention_mask.transpose(-1, -2) > -1)).sum(-2, keepdim=True) /\
                    #     (attention_mask > -1).float().sum(-1, keepdim=True)
                    # average_scale = torch.sigmoid(estimated_scales[..., 1:2])
                    # partial_context_layer = partial_context_layer * average_scale + (1-average_scale) * average_context_layer
                else:
                    attention_probs_dense = partial_attention_probs = attention_scores_dense = None
                    partial_context_layer = q_for_score
                    
                    # TODO HJ Apply probs scaler!
                    
                    # using xFormers
                    # with timer("attention.binary_mask"):
                    #     sparse_attention_mask = partial_attention_mask < -1
                    # N, H, T, HEAD_H = q_for_score.shape
                    # with timer("attention.sparsify"):
                    #     global T_MASK
                    #     if T_MASK is None:
                    #         T_MASK = SparseCS(
                    #             sparse_attention_mask.view(N*H, T, T)[:1, :, :],
                    #             device=q_for_score.device
                    #         )
                    #     sparse_mask = T_MASK
                    # with timer("attention.attention"):
                    #     partial_context_layer = scaled_dot_product_attention(
                    #         q=q_for_score.reshape(N*H, T, HEAD_H),
                    #         k=k_for_score.reshape(N*H, T, HEAD_H),
                    #         v=v.reshape(N*H, T, HEAD_H),
                    #         att_mask=sparse_mask
                    #     )
                    # partial_context_layer = partial_context_layer.view(N, H, T, HEAD_H)
                    
                    # using Numba
                    # N, H, T, HEAD_H = q_for_score.shape
                    # with timer("attention.coo"):
                    #     sparse_attention_mask = partial_attention_mask.view(N*H, T, T).to_sparse_coo()
                    # from .masked_mm import sparse_attn
                    # with timer("attention.sparse"):
                    #     partial_attention_scores = sparse_attn(
                    #         q_for_score.reshape(N*H, T, HEAD_H).contiguous(), 
                    #         k_for_score.reshape(N*H, T, HEAD_H).contiguous(), 
                    #         sparse_attention_mask
                    #     )
                    # with timer("attention.sparse_softmax"):
                    #     partial_attention_probs = torch.sparse.softmax(
                    #         partial_attention_scores, dim=2
                    #     )
                    # with timer("attention.bmm"):
                    #     partial_context_layer = torch.bmm(partial_attention_probs, v.view(N*H, T, HEAD_H))
                    #     partial_context_layer = partial_context_layer.view(N, H, T, HEAD_H)
            
            if self.pconfig.random_lookup:
                # lookup randomly that not looked up by partial context
                num_lookups = self.pconfig.random_lookup_count
                lookups = None
                estimated_attention_probs_masked = estimated_attention_probs * (attention_mask > -1) * (partial_attention_scores > -9999)
                for n in range(num_lookups):
                    token_length = (attention_mask.view(N, T) > -1).float().sum(dim=-1).view(N, 1, 1, 1)
                    # N, H, T, HID
                    random_context_index = torch.rand_like(partial_context_layer)
                    random_context_index = (random_context_index * (1 - 1/T) * token_length).floor().long()
                    
                    random_context_layer = v.gather(dim=-2, index=random_context_index)
                    random_context_weight = estimated_attention_probs_masked.gather(dim=-1, index=random_context_index)
                    random_context_layer = random_context_weight * random_context_layer
                    if lookups is None:
                        lookups = random_context_layer
                    else:
                        lookups = lookups + random_context_layer
                
                random_context_layer = random_context_layer.permute(0, 2, 1, 3).contiguous()
                new_context_layer_shape = random_context_layer.size()[:-2] + (self.all_head_size,)
                random_context_layer = random_context_layer.view(new_context_layer_shape)

            with timer("context_permute"):
                partial_context_layer = partial_context_layer.permute(0, 2, 1, 3).contiguous()
                new_context_layer_shape = partial_context_layer.size()[:-2] + (self.all_head_size,)
                partial_context_layer = partial_context_layer.view(new_context_layer_shape)
                if self.pconfig.out_add_performer_context:
                    performer_context_layer = performer_context_layer.permute(0, 2, 1, 3).contiguous()
                    performer_context_layer = performer_context_layer.view(new_context_layer_shape)
            
            with timer("out"):
                if not self.pconfig.random_lookup:
                    partial_context_layer = \
                        self.norm_partial(partial_context_layer) +\
                        partial_context_layer
                    if self.pconfig.out_add_performer_context:
                        raise Exception('performer context hidden size is modified')
                        partial_context_layer = partial_context_layer +\
                            self.norm_performer(performer_context_layer)
                else:
                    partial_context_layer = \
                        self.norm_partial(partial_context_layer) +\
                        self.norm_random(random_context_layer) +\
                        partial_context_layer
                    if self.pconfig.out_add_performer_context:
                        raise Exception('performer context hidden size is modified')
                        partial_context_layer = partial_context_layer +\
                            self.norm_performer(performer_context_layer)
                
                if self.pconfig.out_norm:
                    partial_context_layer = self.norm(partial_context_layer)
            
            if not self.benchmarking:
                raise_if_nan(context_layer_truth)
                raise_if_nan(partial_context_layer)
                loss += F.mse_loss(
                    context_layer_truth, 
                    partial_context_layer
                )
                raise_if_nan(loss)

            raise_if_nan(loss)
            raise_if_nan(partial_context_layer)
            raise_if_nan(partial_attention_probs)
            raise_if_nan(estimated_attention_probs_resized)
            raise_if_nan(attention_probs_dense)
            raise_if_nan(k_for_score)
            
            return PerlinAttentionOutput(
                loss=loss,
                context_layer=partial_context_layer,
                partial_attention_probs=partial_attention_probs,
                estimated_attention_probs=estimated_attention_probs if self.benchmarking else estimated_attention_probs_resized,
                dense_attention_probs=attention_probs_dense,
                key_for_score=k_for_score,
            )