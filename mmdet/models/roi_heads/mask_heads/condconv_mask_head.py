from typing import Dict
import math

import torch
from torch import nn
import torch.functional as F

from mmcv.cnn import ConvModule

from functools import partial

from mmdet.models.builder import HEADS, build_loss
from mmdet.utils.common import compute_locations
import torch.nn.functional as F

INF = 100000000

@HEADS.register_module()
class CondConvMaskHead(nn.Module):
    def __init__(self, branch_cfg, head_cfg):
        super().__init__()
        self.branch_cfg = branch_cfg
        self.head_cfg = head_cfg
        self.branch = build_mask_branch(branch_cfg)
        head_cfg['in_channels'] = branch_cfg['out_channels']
        self.head = build_dynamic_mask_head(head_cfg)
        self.max_proposals = head_cfg.max_proposals
    def forward(self, features, pred_instances, gt_masks=None, gt_labels=None): # gt_labels for sem_loss_on
        mask_feats, losses = self.branch(features, gt_masks, gt_labels)

        if self.training:
            if 0 <= self.max_proposals < len(pred_instances):
                inds = torch.randperm(len(pred_instances), device=mask_feats.device).long()
                print("clipping proposals from {} to {}".format(
                    len(pred_instances), self.max_proposals
                ))
                pred_instances = pred_instances[inds[:self.max_proposals]]

            loss_mask = self.head(mask_feats, self.branch.out_stride, pred_instances, gt_masks)
            losses.update({"loss_mask": loss_mask})
            return losses
        else:
            pred_mask = self.head(mask_feats, self.branch.out_stride, pred_instances)
            return pred_mask


def build_mask_branch(cfg):
    return MaskBranch(cfg)
            
# modified from https://github.com/aim-uofa/AdelaiDet/blob/0157227f966eda93c1299a402537b616207ba226/adet/modeling/condinst/

def aligned_bilinear(tensor, factor):
    assert tensor.dim() == 4
    assert factor >= 1
    assert int(factor) == factor

    if factor == 1:
        return tensor

    h, w = tensor.size()[2:]
    tensor = F.pad(tensor, pad=(0, 1, 0, 1), mode="replicate")
    oh = factor * h + 1
    ow = factor * w + 1
    tensor = F.interpolate(
        tensor, size=(oh, ow),
        mode='bilinear',
        align_corners=True
    )
    tensor = F.pad(
        tensor, pad=(factor // 2, 0, factor // 2, 0),
        mode="replicate"
    )

    return tensor[:, :, :oh - 1, :ow - 1]


class MaskBranch(nn.Module):
    def __init__(self, cfg):
        super().__init__()
        self.in_features = cfg.in_features
        self.sem_loss_on = cfg.semantic_loss_on
        self.num_outputs = cfg.out_channels
        norm = cfg.norm
        num_convs = cfg.num_convs
        channels = cfg.channels
        self.out_stride = cfg.out_stride[0] # select the highest resolution
        self.in_channels = cfg.in_channels


        self.refine = nn.ModuleList()
        for in_feature in self.in_features:
            self.refine.append(ConvModule(
                self.in_channels,
                channels, 3, 1, padding=1, norm_cfg=norm
            ))

        tower = []
        for i in range(num_convs):
            tower.append(ConvModule(
                channels, channels, 3, 1, padding=1, norm_cfg=norm
            ))
        tower.append(nn.Conv2d(
            channels, max(self.num_outputs, 1), 1
        ))
        self.add_module('tower', nn.Sequential(*tower))

        # TODO: add sem_loss_on support
        if self.sem_loss_on:
            prior_prob = cfg.loss_sem.pop('prior_prob')
            self.loss = build_loss(cfg.loss_sem)

            in_channels = self.in_channels
            self.seg_head = nn.Sequential(
                ConvModule(in_channels, channels, kernel_size=3, stride=1, padding=1, norm_cfg=norm),
                ConvModule(channels, channels, kernel_size=3, stride=1, padding=1, norm_cfg=norm)
            )

            num_classes = cfg.num_classes
            self.logits = nn.Conv2d(channels, num_classes, kernel_size=1, stride=1)

            bias_value = -math.log((1 - prior_prob) / prior_prob)
            torch.nn.init.constant_(self.logits.bias, bias_value)

    def forward(self, features, gt_masks=None, gt_labels=None):
        for i, f in enumerate(self.in_features):
            if i == 0:
                x = self.refine[i](features[f])
            else:
                x_p = self.refine[i](features[f])

                target_h, target_w = x.size()[2:]
                h, w = x_p.size()[2:]
                assert target_h % h == 0
                assert target_w % w == 0
                factor_h, factor_w = target_h // h, target_w // w
                assert factor_h == factor_w
                x_p = aligned_bilinear(x_p, factor_h) # TODO: different bilinear interpolate
                # x_p = F.interpolate(x_p, size=(target_h,target_w), mode='bilinear', align_corners=True)
                x = x + x_p

        mask_feats = self.tower(x)

        if self.num_outputs == 0:
            mask_feats = mask_feats[:, :self.num_outputs]

        losses = {}
        # auxiliary thing semantic loss
        if self.training and self.sem_loss_on:
            logits_pred = self.logits(self.seg_head(
                features[self.in_features[0]]
            ))

            # compute semantic targets
            semantic_targets = []
            for gt_mask, gt_label in zip(gt_masks,gt_labels):
                gt_mask = torch.tensor(gt_mask.masks, device=logits_pred.device)
                h, w = gt_mask.size()[-2:]
                areas = gt_mask.sum(dim=-1).sum(dim=-1)
                areas = areas[:, None, None].repeat(1, h, w)
                areas[gt_mask == 0] = INF
                areas = areas.permute(1, 2, 0).reshape(h * w, -1)
                min_areas, inds = areas.min(dim=1)
                per_im_sematic_targets = gt_label[inds] + 1
                per_im_sematic_targets[min_areas == INF] = 0
                per_im_sematic_targets = per_im_sematic_targets.reshape(h, w)
                per_im_sematic_targets = F.interpolate(per_im_sematic_targets.view(1,1,h,w).to(dtype=torch.float), size=logits_pred.shape[-2:], mode='nearest')
                semantic_targets.append(per_im_sematic_targets)

            semantic_targets = torch.cat(semantic_targets, dim=0)

            # prepare one-hot targets
            num_classes = logits_pred.size(1)
            class_range = torch.arange(
                num_classes, dtype=logits_pred.dtype,
                device=logits_pred.device
            )[:, None, None]
            class_range = class_range + 1
            one_hot = (semantic_targets == class_range).float()
            num_pos = (one_hot > 0).sum().float().clamp(min=1.0)
            loss_sem = self.loss(logits_pred.permute(0,2,3,1).reshape(-1, num_classes), one_hot.permute(0,2,3,1).reshape(-1, num_classes).to(torch.long), avg_factor=num_pos)
            losses['loss_sem'] = loss_sem

        return mask_feats, losses


# dice loss
def dice_coefficient(x, target):
    eps = 1e-5
    n_inst = x.size(0)
    x = x.reshape(n_inst, -1)
    target = target.reshape(n_inst, -1)
    intersection = (x * target).sum(dim=1)
    union = (x ** 2.0).sum(dim=1) + (target ** 2.0).sum(dim=1) + eps
    loss = 1. - (2 * intersection / union)
    return loss


def parse_dynamic_params(params, channels, weight_nums, bias_nums):
    assert params.dim() == 2
    assert len(weight_nums) == len(bias_nums)
    assert params.size(1) == sum(weight_nums) + sum(bias_nums)

    num_insts = params.size(0)
    num_layers = len(weight_nums)

    params_splits = list(torch.split_with_sizes(
        params, weight_nums + bias_nums, dim=1
    ))

    weight_splits = params_splits[:num_layers]
    bias_splits = params_splits[num_layers:]

    for l in range(num_layers):
        if l < num_layers - 1:
            # out_channels x in_channels x 1 x 1
            weight_splits[l] = weight_splits[l].reshape(num_insts * channels, -1, 1, 1)
            bias_splits[l] = bias_splits[l].reshape(num_insts * channels)
        else:
            # out_channels x in_channels x 1 x 1
            weight_splits[l] = weight_splits[l].reshape(num_insts * 1, -1, 1, 1)
            bias_splits[l] = bias_splits[l].reshape(num_insts)

    return weight_splits, bias_splits


def build_dynamic_mask_head(cfg):
    return DynamicMaskHead(cfg)


class DynamicMaskHead(nn.Module):
    def __init__(self, cfg):
        super().__init__()
        self.num_layers = cfg.num_layers
        self.channels = cfg.channels
        self.in_channels = cfg.in_channels
        self.mask_out_stride = cfg.mask_out_stride
        self.disable_rel_coords = cfg.disable_rel_coords

        soi = cfg.sizes_of_interest
        self.register_buffer("sizes_of_interest", torch.tensor(soi))

        

        weight_nums, bias_nums = [], []
        for l in range(self.num_layers):
            if l == 0:
                if not self.disable_rel_coords:
                    weight_nums.append((self.in_channels + 2) * self.channels)
                else:
                    weight_nums.append(self.in_channels * self.channels)
                bias_nums.append(self.channels)
            elif l == self.num_layers - 1:
                weight_nums.append(self.channels * 1)
                bias_nums.append(1)
            else:
                weight_nums.append(self.channels * self.channels)
                bias_nums.append(self.channels)

        self.weight_nums = weight_nums
        self.bias_nums = bias_nums
        self.num_gen_params = sum(weight_nums) + sum(bias_nums)

    def mask_heads_forward(self, features, weights, biases, num_insts):
        '''
        :param features
        :param weights: [w0, w1, ...]
        :param bias: [b0, b1, ...]
        :return:
        '''
        assert features.dim() == 4
        n_layers = len(weights)
        x = features
        for i, (w, b) in enumerate(zip(weights, biases)):
            x = F.conv2d(
                x, w, bias=b,
                stride=1, padding=0,
                groups=num_insts
            )
            if i < n_layers - 1:
                x = F.relu(x)
        return x

    def mask_heads_forward_with_coords(
            self, mask_feats, mask_feat_stride, instances
    ):
        locations = compute_locations(
            mask_feats.size(2), mask_feats.size(3),
            stride=mask_feat_stride, device=mask_feats.device
        )
        n_inst = len(instances)

        im_inds = instances.im_inds
        mask_head_params = instances.mask_head_params

        N, _, H, W = mask_feats.size()

        if not self.disable_rel_coords:
            instance_locations = instances.locations
            relative_coords = instance_locations.reshape(-1, 1, 2) - locations.reshape(1, -1, 2)
            relative_coords = relative_coords.permute(0, 2, 1).float()
            soi = self.sizes_of_interest.float()[instances.fpn_levels]
            relative_coords = relative_coords / soi.reshape(-1, 1, 1)
            relative_coords = relative_coords.to(dtype=mask_feats.dtype)

            mask_head_inputs = torch.cat([
                relative_coords, mask_feats[im_inds].reshape(n_inst, self.in_channels, H * W)
            ], dim=1)
        else:
            mask_head_inputs = mask_feats[im_inds].reshape(n_inst, self.in_channels, H * W)

        mask_head_inputs = mask_head_inputs.reshape(1, -1, H, W)

        weights, biases = parse_dynamic_params(
            mask_head_params, self.channels,
            self.weight_nums, self.bias_nums
        )

        mask_logits = self.mask_heads_forward(mask_head_inputs, weights, biases, n_inst)

        mask_logits = mask_logits.reshape(-1, 1, H, W)

        assert mask_feat_stride >= self.mask_out_stride
        assert mask_feat_stride % self.mask_out_stride == 0
        mask_logits = aligned_bilinear(mask_logits, int(mask_feat_stride / self.mask_out_stride))

        return mask_logits.sigmoid()

    def __call__(self, mask_feats, mask_feat_stride, pred_instances, gt_masks=None):
        if self.training:

            if len(pred_instances) == 0:
                loss_mask = mask_feats.sum() * 0 + pred_instances.mask_head_params.sum() * 0
            else:
                mask_scores = self.mask_heads_forward_with_coords(
                    mask_feats, mask_feat_stride, pred_instances
                )
                gt_inds = pred_instances.gt_inds
                gt_bitmasks = torch.cat([F.interpolate(gt_mask.to_tensor(dtype=mask_scores.dtype, device=mask_scores.device).unsqueeze(1), size=mask_scores.shape[-2:], mode='nearest') for gt_mask in gt_masks])
                gt_bitmasks = gt_bitmasks[gt_inds].to(dtype=mask_feats.dtype)
                mask_losses = dice_coefficient(mask_scores, gt_bitmasks)
                loss_mask = mask_losses.mean()

            return loss_mask.float()
        else:
            if len(pred_instances) > 0:
                mask_scores = self.mask_heads_forward_with_coords(
                    mask_feats, mask_feat_stride, pred_instances
                ).float()
            else:
                mask_scores = None
            return mask_scores
