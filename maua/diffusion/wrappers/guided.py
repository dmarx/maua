import os
import sys
from dataclasses import dataclass
from functools import partial

import torch
from tqdm import tqdm

from ...utility import download
from ..conditioning import FastGradientGuidedConditioning, GradientGuidedConditioning
from .base import DiffusionWrapper

sys.path += ["maua/submodules/guided_diffusion"]

from guided_diffusion.script_util import create_model_and_diffusion, model_and_diffusion_defaults


def append_dims(x, n):
    return x[(Ellipsis, *(None,) * (n - x.ndim))]


def expand_to_planes(x, shape):
    return append_dims(x, len(shape)).repeat([1, 1, *shape[2:]])


def t_to_alpha_sigma(t):
    return torch.cos(t * torch.pi / 2), torch.sin(t * torch.pi / 2)


@dataclass
class DiffusionOutput:
    v: torch.Tensor
    pred: torch.Tensor
    eps: torch.Tensor


class ConvBlock(torch.nn.Sequential):
    def __init__(self, c_in, c_out):
        super().__init__(
            torch.nn.Conv2d(c_in, c_out, 3, padding=1),
            torch.nn.ReLU(inplace=True),
        )


class SkipBlock(torch.nn.Module):
    def __init__(self, main, skip=None):
        super().__init__()
        self.main = torch.nn.Sequential(*main)
        self.skip = skip if skip else torch.nn.Identity()

    def forward(self, input):
        return torch.cat([self.main(input), self.skip(input)], dim=1)


class FourierFeatures(torch.nn.Module):
    def __init__(self, in_features, out_features, std=1.0):
        super().__init__()
        assert out_features % 2 == 0
        self.weight = torch.nn.Parameter(torch.randn([out_features // 2, in_features]) * std)

    def forward(self, input):
        f = 2 * torch.pi * input @ self.weight.T
        return torch.cat([f.cos(), f.sin()], dim=-1)


class SecondaryDiffusionImageNet2(torch.nn.Module):
    def __init__(self):
        super().__init__()
        c = 64  # The base channel count
        cs = [c, c * 2, c * 2, c * 4, c * 4, c * 8]

        self.timestep_embed = FourierFeatures(1, 16)
        self.down = torch.nn.AvgPool2d(2)
        self.up = torch.nn.Upsample(scale_factor=2, mode="bilinear", align_corners=False)

        self.net = torch.nn.Sequential(
            ConvBlock(3 + 16, cs[0]),
            ConvBlock(cs[0], cs[0]),
            SkipBlock(
                [
                    self.down,
                    ConvBlock(cs[0], cs[1]),
                    ConvBlock(cs[1], cs[1]),
                    SkipBlock(
                        [
                            self.down,
                            ConvBlock(cs[1], cs[2]),
                            ConvBlock(cs[2], cs[2]),
                            SkipBlock(
                                [
                                    self.down,
                                    ConvBlock(cs[2], cs[3]),
                                    ConvBlock(cs[3], cs[3]),
                                    SkipBlock(
                                        [
                                            self.down,
                                            ConvBlock(cs[3], cs[4]),
                                            ConvBlock(cs[4], cs[4]),
                                            SkipBlock(
                                                [
                                                    self.down,
                                                    ConvBlock(cs[4], cs[5]),
                                                    ConvBlock(cs[5], cs[5]),
                                                    ConvBlock(cs[5], cs[5]),
                                                    ConvBlock(cs[5], cs[4]),
                                                    self.up,
                                                ]
                                            ),
                                            ConvBlock(cs[4] * 2, cs[4]),
                                            ConvBlock(cs[4], cs[3]),
                                            self.up,
                                        ]
                                    ),
                                    ConvBlock(cs[3] * 2, cs[3]),
                                    ConvBlock(cs[3], cs[2]),
                                    self.up,
                                ]
                            ),
                            ConvBlock(cs[2] * 2, cs[2]),
                            ConvBlock(cs[2], cs[1]),
                            self.up,
                        ]
                    ),
                    ConvBlock(cs[1] * 2, cs[1]),
                    ConvBlock(cs[1], cs[0]),
                    self.up,
                ]
            ),
            ConvBlock(cs[0] * 2, cs[0]),
            torch.nn.Conv2d(cs[0], 3, 3, padding=1),
        )

    def forward(self, input, t):
        timestep_embed = expand_to_planes(self.timestep_embed(t[:, None]), input.shape)
        v = self.net(torch.cat([input, timestep_embed], dim=1))
        alphas, sigmas = map(partial(append_dims, n=v.ndim), t_to_alpha_sigma(t))
        pred = input * alphas - v * sigmas
        eps = input * sigmas + v * alphas
        return DiffusionOutput(v, pred, eps)


def get_checkpoint(checkpoint_name):
    if checkpoint_name == "uncondImageNet512":
        checkpoint_path = "modelzoo/512x512_diffusion_uncond_finetune_008100.pt"
        if not os.path.exists(checkpoint_path):
            download(
                "https://the-eye.eu/public/AI/models/512x512_diffusion_unconditional_ImageNet/512x512_diffusion_uncond_finetune_008100.pt",
                checkpoint_path,
            )
        checkpoint_config = {"image_size": 512}
    elif checkpoint_name == "uncondImageNet256":
        checkpoint_path = "modelzoo/256x256_diffusion_uncond.pt"
        if not os.path.exists(checkpoint_path):
            download(
                "https://openaipublic.blob.core.windows.net/diffusion/jul-2021/256x256_diffusion_uncond.pt",
                checkpoint_path,
            )
        checkpoint_config = {"image_size": 256}
    return checkpoint_path, checkpoint_config


def create_models(
    checkpoint="uncondImageNet512",
    timestep_respacing="100",
    diffusion_steps=1000,
    use_secondary=False,
):
    checkpoint_path, checkpoint_config = get_checkpoint(checkpoint)
    model_config = model_and_diffusion_defaults()
    model_config.update(
        {
            "attention_resolutions": "32, 16, 8",
            "class_cond": False,
            "diffusion_steps": diffusion_steps,
            "rescale_timesteps": True,
            "timestep_respacing": timestep_respacing,
            "learn_sigma": True,
            "noise_schedule": "linear",
            "num_channels": 256,
            "num_head_channels": 64,
            "num_res_blocks": 2,
            "resblock_updown": True,
            "use_fp16": True,
            "use_scale_shift_norm": True,
        }
    )
    model_config.update(checkpoint_config)
    diffusion_model, diffusion = create_model_and_diffusion(**model_config)
    diffusion_model.load_state_dict(torch.load(checkpoint_path, map_location="cpu"))
    diffusion_model.requires_grad_(False).eval()
    for name, param in diffusion_model.named_parameters():
        if "qkv" in name or "norm" in name or "proj" in name:
            param.requires_grad_()
    if model_config["use_fp16"]:
        diffusion_model.convert_to_fp16()

    if use_secondary:
        checkpoint_path = "modelzoo/secondary_model_imagenet_2.pth"
        if not os.path.exists(checkpoint_path):
            download("https://the-eye.eu/public/AI/models/v-diffusion/secondary_model_imagenet_2.pth", checkpoint_path)
        secondary_model = SecondaryDiffusionImageNet2()
        secondary_model.load_state_dict(torch.load(checkpoint_path, map_location="cpu"))
        secondary_model.eval().requires_grad_(False)
    else:
        secondary_model = None

    return diffusion_model, diffusion, secondary_model


class GuidedDiffusion(DiffusionWrapper):
    def __init__(
        self,
        grad_modules,
        sampler="ddim",
        timesteps=100,
        model_checkpoint="uncondImageNet512",
        device=torch.device("cuda" if torch.cuda.is_available() else "cpu"),
        ddim_eta=0,
        plms_order=2,
        fast=True,
    ):
        super().__init__()
        self.model, self.diffusion, secondary_model = create_models(
            checkpoint=model_checkpoint,
            timestep_respacing=f"ddim{timesteps}" if sampler == "ddim" else str(timesteps),
            use_secondary=fast,
        )
        self.conditioning = (FastGradientGuidedConditioning if fast else GradientGuidedConditioning)(
            self.diffusion, secondary_model if fast else self.model, grad_modules
        )

        if sampler == "p":
            self.sample_fn = lambda _: partial(self.diffusion.p_sample, clip_denoised=False)
        elif sampler == "ddim":
            self.sample_fn = lambda _: partial(self.diffusion.ddim_sample, eta=ddim_eta, clip_denoised=False)
        elif sampler == "plms":
            self.sample_fn = lambda old_out: partial(
                self.diffusion.plms_sample, order=plms_order, old_out=old_out, clip_denoised=False
            )

        self.device = device
        self.model = self.model.to(device)
        self.conditioning = self.conditioning.to(device)

    @torch.no_grad()
    def sample(
        self,
        img,
        prompts,
        start_step,
        n_steps,
        model_kwargs={},
        randomize_class=False,
        verbose=True,
        q_sample=None,
        noise=None,
    ):
        self.conditioning.set_targets(prompts)

        if q_sample is None:
            q_sample = start_step
        if q_sample > 0:
            t = torch.ones([img.shape[0]], device=self.device, dtype=torch.long) * q_sample - 1
            img = self.diffusion.q_sample(img, t, noise)

        steps = range(start_step - 1, start_step - n_steps - 1, -1)
        if verbose:
            steps = tqdm(steps)

        out = None
        for i in steps:
            if randomize_class and "y" in model_kwargs:
                model_kwargs["y"] = torch.randint(
                    low=0, high=self.model.num_classes, size=model_kwargs["y"].shape, device=self.device
                )

            t = torch.tensor([i] * img.shape[0], device=self.device, dtype=torch.long)
            out = self.sample_fn(out)(self.model, img, t, cond_fn=self.conditioning, model_kwargs=model_kwargs)
            img = out["sample"]

        return img

    def forward(self, shape, prompts, model_kwargs={}):
        img = torch.randn(*shape, device=self.device)
        steps = len(self.diffusion.use_timesteps)
        return self.sample(img, prompts, start_step=steps, n_steps=steps, model_kwargs=model_kwargs)
