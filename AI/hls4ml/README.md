# Converting a model to an FPGA overlay (WSL)

This stage runs in WSL (Ubuntu 22.04), not on Windows or the board. It turns a trained `.h5` into a
Vitis HLS project, checks the fixed-point C model against training window by window, synthesises
the IP, and ends with an `.xsa` overlay for [ultra96/](../ultra96/README.md).

| File | Job |
|---|---|
| `convert.py` | `.h5` → hls4ml project (Vitis backend, `io_stream`, Ultra96 part `xczu3eg-sbva484-1-i`, 100 MHz), the per-layer precisions, the checks, and a golden testbench |
| `templates/cnn_axi.cpp` | The top function: an AXI-stream wrapper that reads 150 × 6 16-bit beats, runs the network, and writes one 16-bit logit per class with TLAST on the last |
| `templates/cnn_axi_test.cpp` | Its C/RTL testbench: compares the wrapper's logits on `tb_data/` against `hls_model.predict()` (passes if the argmax matches) |
| `templates/build_prj.tcl`, `build_opt.tcl` | hls4ml's build script with `cnn_axi` as the top; runs csim, synthesis, cosim and IP export |

`convert.py` copies the templates into the new project with `N_T` (timesteps) and `N_OUT` (classes)
set from the model, so nothing is copied by hand from an older project.

## Steps

**1. Get the inputs into WSL.** From Windows, after [train](../train/README.md) stages 3 and 4:
`train/results/models/<tag>_s<seed>.h5` and `train/results/test_windows_<tag>.npz`. Copy both into
the WSL home folder (`\\wsl.localhost\Ubuntu-22.04\home\<user>\`).

**2. Convert and check** (in WSL, from the home folder):
```bash
conda activate hls4ml-tutorial
python /mnt/c/<path-to-repo>/AI/hls4ml/convert.py --model <tag>_s<seed>.h5 \
    --npz test_windows_<tag>.npz --out ~/<tag>
```
Keep `--out` in the WSL home folder: Vitis builds are slow on `/mnt/c`.

| Flag | Meaning |
|---|---|
| `--reuse-factor N` | Default 4096. hls4ml rounds it to the nearest valid value per layer (it prints a warning for each). Raise it to use fewer DSPs |
| `--threshold P` | The comms `CONF_THRESHOLD` (0.7), for the event / no-event agreement check |
| `--tb-per-set N` | Testbench windows taken from each test set (default 7) |
| `--trace` | hls4ml Trace on every layer |
| `--clock-period NS` | Default 10 (100 MHz) |
| `--no-templates` | Don't copy `templates/` in |

It prints, in order:

- **(A)–(D)** the precisions per layer, the weight ranges, the Keras activation ranges, and Keras vs
  HLS per layer, which catches saturation. Figures go to `<out>/profile_run/` when hls4ml's
  profiling works.
- **(1)** the Dropout-stripped Keras model vs the `.h5`'s saved outputs. Expect 0.
- **(2)** the HLS C model vs Keras on every test window: logit difference, argmax agreement, and
  event / no-event agreement under the comms rule.
- **(3)** accuracy per test set. Keras must equal the run's `run_<tag>.json`; HLS should be within a
  window or two.

For the deployed `mage_noise_robust_s2.h5`: (1) 0.000000; (2) argmax agrees on 4,568 of 4,569
windows, events on 99.8%; (3) jitter 0.9473 / 0.9473, slide_bg 0.9738 / 0.9742, noise
0.9766 / 0.9766 (Keras / HLS).

The precisions (`RECO` in `convert.py`) use the layer names in `train/model.py`: 16-bit results with
6 or 7 integer bits, a 24-bit accumulator, and `ap_fixed<16,6>` input and output, which is the board's
`int16 = round(x × 1024)`. Re-check tables (C) and (D) after changing the architecture.

**3. Synthesise the IP:**
```bash
cd ~/<tag>
vitis_hls -f build_prj.tcl
```
`build_opt.tcl` turns on csim, synthesis, cosim and export. Both simulations run
`cnn_axi_test.cpp` on `tb_data/` and print `PASS` when the argmax matches the golden output. The
exported IP is in `cnn_prj/solution1/impl/ip/`.

**4. Build the overlay in Vivado.** This step is done by hand in the Vivado GUI (`vivado`). The
block-design project is `~/cnn_25th_sep/vivado_prj/vivado_prj.xpr`, whose `design_1` holds the Zynq
UltraScale+ PS, an AXI DMA and the `cnn_axi` IP (`xilinx.com:hls:cnn_axi:1.0`):

1. Point the project's IP repository at the new `~/<tag>/cnn_prj/solution1/impl/ip`, then upgrade
   the `cnn_axi` IP in `design_1`.
2. Generate the bitstream, then **File → Export → Export Hardware** with the bitstream included.
   Earlier exports are kept beside the project as `vivado_prj/*.xsa`.

`wk10_integration.py` looks the IP up by instance name, so keep them as `axi_dma_0` (MM2S carries
the 900 input beats, S2MM the logits) and `cnn_axi_0` (its `s_axilite` control port gets an
`ap_start` pulse per inference).

**5. Deploy.** Copy the exported `.xsa` to `AI/ultra96/gesture_model.xsa` and follow
[ultra96/README.md](../ultra96/README.md#deploying-a-new-overlay).
