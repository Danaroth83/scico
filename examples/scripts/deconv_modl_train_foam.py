#!/usr/bin/env python
# -*- coding: utf-8 -*-
# This file is part of the SCICO package. Details of the copyright
# and user license can be found in the 'LICENSE.txt' file distributed
# with the package.

r"""
Training of MoDL for Deconvolution
==================================

This example demonstrates the training and application of a model-based deep learning
(MoDL) architecture described in :cite:`aggarwal-2019-modl`
 for a deconvolution (deblurring) problem.

The source images are foam phantoms generated with xdesign.

A class [flax.MoDLNet](../_autosummary/scico.learning.rst#scico.learning.MoDL)
 implements the MoDL architecture, which solves the optimization problem

  $$\mathrm{argmin}_{\mathbf{x}} \; \| A \mathbf{x} - \mathbf{y} \|_2^2 + \lambda \, \| \mathbf{x} - \mathrm{D}_w(\mathbf{x})\|_2^2 \;,$$

where $A$ is a circular convolution, $\mathbf{y}$ is a set of blurred images, $\mathrm{D}_w$ is the
 regularization (a denoiser), and $\mathbf{x}$ is the set of deblurred images.
  The MoDL abstracts the iterative solution by an unrolled network where each iteration corresponds
  to a different stage in the MoDL network and updates the prediction by solving

  $$\mathbf{x}^{k+1} = (A^T A + \lambda \, I)^{-1} (A^T \mathbf{y} + \lambda \, \mathbf{z}^k) \;,$$

via conjugate gradient. In the expression, $k$ is the index of the stage (iteration),
 $\mathbf{z}^k = \mathrm{ResNet}(\mathbf{x}^{k})$ is the regularization
 (a denoiser implemented as a residual convolutional neural network), $\mathbf{x}^k$ is the output
  of the previous stage, $\lambda > 0$
  is a learned regularization parameter, and $I$ is the identity operator.
  The output of the final stage is the set of deblurred images.
"""

import os
from functools import partial
from time import time

import jax
import jax.numpy as jnp

from mpl_toolkits.axes_grid1 import make_axes_locatable

from scico import flax as sflax
from scico import metric, plot
from scico.flax.examples import load_foam_blur_data
from scico.flax.train.train import clip_positive, construct_traversal
from scico.linop import CircularConvolve

"""
Prepare parallel processing. Set an arbitrary processor
count (only applies if GPU is not available).
"""
os.environ["XLA_FLAGS"] = "--xla_force_host_platform_device_count=8"
platform = jax.lib.xla_bridge.get_backend().platform
print("Platform: ", platform)

"""
Define blur operator.
"""
output_size = 256  # image size

n = 3  # convolution kernel size
σ = 20.0 / 255  # noise level
psf = jnp.ones((n, n)) / (n * n)  # blur kernel

ishape = (output_size, output_size)
opBlur = CircularConvolve(h=psf, input_shape=ishape)

opBlur_vmap = jax.vmap(opBlur)  # for batch processing in data generation

"""
Read data from cache or generate if not available.
"""
train_nimg = 416  # number of training images
test_nimg = 64  # number of testing images
nimg = train_nimg + test_nimg

train_ds, test_ds = load_foam_blur_data(
    train_nimg,
    test_nimg,
    output_size,
    psf,
    σ,
    verbose=True,
)


"""
Define configuration dictionary for model and training loop.

Parameters have been selected for demonstration purposes and relatively short training.
The model depth is akin to the number of unrolled iterations in the MoDL model.
The block depth controls the number of layers at each unrolled iteration.
The number of filters is uniform throughout the iterations.
The iterations used for the conjugate gradient (CG) solver can also be specified.
Better performance may be obtained by increasing depth, block depth, number of filters, CG iterations, or training epochs,
but may require longer training times.
"""
# model configuration
model_conf = {
    "depth": 2,
    "num_filters": 64,
    "block_depth": 4,
    "cg_iter": 4,
}
# training configuration
train_conf: sflax.ConfigDict = {
    "seed": 0,
    "opt_type": "SGD",
    "momentum": 0.9,
    "batch_size": 16,
    "num_epochs": 25,
    "base_learning_rate": 1e-2,
    "warmup_epochs": 0,
    "log_every_steps": 100,
    "log": True,
}

"""
Construct functionality for making sure that
the learned regularization parameter is always
positive.
"""
lmbdatrav = construct_traversal("lmbda")  # select lmbda parameters in model
lmbdapos = partial(
    clip_positive,  # apply this function
    traversal=lmbdatrav,  # to lmbda parameters in model
    minval=5e-4,
)


"""
Print configuration of distributed run.
"""
print(f"{'JAX process: '}{jax.process_index()}{' / '}{jax.process_count()}")
print(f"{'JAX local devices: '}{jax.local_devices()}")

"""
Check for iterated trained model. If not found, construct MoDLNet model, using only one iteration
(depth) in model and few CG iterations for faster intialization. Run first stage
(initialization) training loop
followed by a second stage (depth iterations) training loop.
"""
channels = train_ds["image"].shape[-1]
workdir2 = os.path.join(
    os.path.expanduser("~"), ".cache", "scico", "examples", "modl_dcnv_out", "iterated"
)

stats_object_ini = None

checkpoint_files = []
for (dirpath, dirnames, filenames) in os.walk(workdir2):
    checkpoint_files = [fn for fn in filenames if str.split(fn, "_")[0] == "checkpoint"]

if len(checkpoint_files) > 0:
    model = sflax.MoDLNet(
        operator=opBlur,
        depth=model_conf["depth"],
        channels=channels,
        num_filters=model_conf["num_filters"],
        block_depth=model_conf["block_depth"],
        cg_iter=model_conf["cg_iter"],
    )

    train_conf["workdir"] = workdir2
    train_conf["post_lst"] = [lmbdapos]
    # Construct training object
    trainer = sflax.BasicFlaxTrainer(
        train_conf,
        model,
        train_ds,
        test_ds,
    )
    start_time = time()
    modvar, stats_object = trainer.train()
    time_train = time() - start_time
    time_init = 0.0
    epochs_init = 0
else:
    # One iteration (depth) in model and few CG iterations
    model = sflax.MoDLNet(
        operator=opBlur,
        depth=1,
        channels=channels,
        num_filters=model_conf["num_filters"],
        block_depth=model_conf["block_depth"],
        cg_iter=model_conf["cg_iter"],
    )
    # First stage: initialization training loop.
    workdir = os.path.join(os.path.expanduser("~"), ".cache", "scico", "examples", "modl_dcnv_out")

    train_conf["workdir"] = workdir
    train_conf["post_lst"] = [lmbdapos]
    # Construct training object
    trainer = sflax.BasicFlaxTrainer(
        train_conf,
        model,
        train_ds,
        test_ds,
    )

    start_time = time()
    modvar, stats_object_ini = trainer.train()
    time_init = time() - start_time
    epochs_init = train_conf["num_epochs"]

    print(
        f"{'MoDLNet init':18s}{'epochs:':2s}{train_conf['num_epochs']:>5d}{'':3s}"
        f"{'time[s]:':21s}{time_init:>7.2f}"
    )

    # Second stage: depth iterations training loop.
    model.depth = model_conf["depth"]
    train_conf["workdir"] = workdir2
    # Construct training object, include current model parameters
    trainer = sflax.BasicFlaxTrainer(
        train_conf,
        model,
        train_ds,
        test_ds,
        variables0=modvar,
    )

    start_time = time()
    modvar, stats_object = trainer.train()
    time_train = time() - start_time

"""
Evaluate on testing data.
"""
del train_ds["image"]
del train_ds["label"]
start_time = time()
fmap = sflax.FlaxMap(model, modvar)
output = fmap(test_ds["image"])
time_eval = time() - start_time
output = jnp.clip(output, a_min=0, a_max=1.0)

"""
Compare trained model in terms of reconstruction time
and data fidelity.
"""
total_epochs = epochs_init + train_conf["num_epochs"]
total_time_train = time_init + time_train
snr_eval = metric.snr(test_ds["label"], output)
psnr_eval = metric.psnr(test_ds["label"], output)
print(
    f"{'MoDLNet training':18s}{'epochs:':2s}{total_epochs:>5d}{'':21s}{'time[s]:':10s}{total_time_train:>7.2f}"
)
print(
    f"{'MoDLNet testing':18s}{'SNR:':5s}{snr_eval:>5.2f}{' dB'}{'':3s}{'PSNR:':6s}{psnr_eval:>5.2f}{' dB'}{'':3s}{'time[s]:':10s}{time_eval:>7.2f}"
)

"""
Plot comparison.
"""
key = jax.random.PRNGKey(54321)
indx = jax.random.randint(key, (1,), 0, test_nimg)[0]

fig, ax = plot.subplots(nrows=1, ncols=3, figsize=(15, 5))
plot.imview(test_ds["label"][indx, ..., 0], title="Ground truth", cbar=None, fig=fig, ax=ax[0])
plot.imview(
    test_ds["image"][indx, ..., 0],
    title="Blurred: \nSNR: %.2f (dB), PSNR: %.2f"
    % (
        metric.snr(test_ds["label"][indx, ..., 0], test_ds["image"][indx, ..., 0]),
        metric.psnr(test_ds["label"][indx, ..., 0], test_ds["image"][indx, ..., 0]),
    ),
    cbar=None,
    fig=fig,
    ax=ax[1],
)
plot.imview(
    output[indx, ..., 0],
    title="MoDLNet Reconstruction\nSNR: %.2f (dB), PSNR: %.2f"
    % (
        metric.snr(test_ds["label"][indx, ..., 0], output[indx, ..., 0]),
        metric.psnr(test_ds["label"][indx, ..., 0], output[indx, ..., 0]),
    ),
    fig=fig,
    ax=ax[2],
)
divider = make_axes_locatable(ax[2])
cax = divider.append_axes("right", size="5%", pad=0.2)
fig.colorbar(ax[2].get_images()[0], cax=cax, label="arbitrary units")
fig.show()

"""
Plot convergence statistics. Statistics only generated if a training cycle was done (i.e. not reading final epoch results from checkpoint).
"""
if stats_object is not None:
    hist = stats_object.history(transpose=True)
    fig, ax = plot.subplots(nrows=1, ncols=2, figsize=(12, 5))
    plot.plot(
        jnp.vstack((hist.Train_Loss, hist.Eval_Loss)).T,
        x=hist.Epoch,
        ptyp="semilogy",
        title="Loss function",
        xlbl="Epoch",
        ylbl="Loss value",
        lgnd=("Train", "Test"),
        fig=fig,
        ax=ax[0],
    )
    plot.plot(
        jnp.vstack((hist.Train_SNR, hist.Eval_SNR)).T,
        x=hist.Epoch,
        title="Metric",
        xlbl="Epoch",
        ylbl="SNR (dB)",
        lgnd=("Train", "Test"),
        fig=fig,
        ax=ax[1],
    )
    fig.show()

# Stats for initialization loop
if stats_object_ini is not None:
    hist = stats_object_ini.history(transpose=True)
    fig, ax = plot.subplots(nrows=1, ncols=2, figsize=(12, 5))
    plot.plot(
        jnp.vstack((hist.Train_Loss, hist.Eval_Loss)).T,
        x=hist.Epoch,
        ptyp="semilogy",
        title="Loss function - Initialization",
        xlbl="Epoch",
        ylbl="Loss value",
        lgnd=("Train", "Test"),
        fig=fig,
        ax=ax[0],
    )
    plot.plot(
        jnp.vstack((hist.Train_SNR, hist.Eval_SNR)).T,
        x=hist.Epoch,
        title="Metric - Initialization",
        xlbl="Epoch",
        ylbl="SNR (dB)",
        lgnd=("Train", "Test"),
        fig=fig,
        ax=ax[1],
    )
    fig.show()


input("\nWaiting for input to close figures and exit")
