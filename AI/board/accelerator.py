"""The CNN accelerator on the PL, as a module other code can import.

The same steps as infer_timed() in test_timing.py, with nothing run at import time:
loading the overlay is a method call, and pynq is imported only then. The Comms board
client imports this to classify gesture windows.

    acc = Accelerator("gesture_min_q.xsa")
    logits = acc.infer([to_fixed(x) for x in row], timeout=0.05)
"""
import time

TIMESTEPS, CHANNELS, N_CLASSES = 100, 6, 7   # input is TIMESTEPS x CHANNELS, time-major
FRAC_BITS = 10      # inputs are Q6.10: ap_fixed<16,6>
OUT_FRAC = 10       # logits too; use 8 if the output is widened to <16,8>
I16MIN, I16MAX = -32768, 32767


class DmaTimeout(RuntimeError):
    """A DMA channel was still busy at the deadline."""


def to_fixed(x):
    """One float -> its Q6.10 int16 word: x 1024, rounded, saturated."""
    return min(max(round(x * (1 << FRAC_BITS)), I16MIN), I16MAX)


class Accelerator:
    """One loaded overlay and its DMA buffers. infer() runs one frame."""

    def __init__(self, bitstream):
        from pynq import Overlay, allocate          # only on the board

        ol = Overlay(str(bitstream))
        if 'cnn_axi_0' not in ol.ip_dict:
            raise RuntimeError(f"{bitstream}: cnn_axi_0 has no AXI-Lite control -- are you "
                               "on the OLD ap_ctrl_none bitstream? Rebuild with s_axilite (3.0).")
        self.dma = ol.axi_dma_0
        self.cnn = ol.cnn_axi_0                     # the IP's control port
        self.inb = allocate(shape=(TIMESTEPS * CHANNELS,), dtype="int16", cacheable=False)
        self.outb = allocate(shape=(N_CLASSES,), dtype="int16", cacheable=False)

    def infer(self, words, timeout=3.0):
        """600 Q6.10 words, time-major -> the 7 logits as floats.

        Busy-polls the DMA with no sleep. Raises DmaTimeout if it is not done in `timeout` s.
        """
        self.inb[:] = words
        self.cnn.write(0x00, 0x01)                  # start the CNN for one frame
        self.dma.recvchannel.transfer(self.outb)    # get ready to receive before sending the input
        self.dma.sendchannel.transfer(self.inb)
        deadline = time.perf_counter() + timeout
        for name, ch in (("SEND", self.dma.sendchannel), ("RECV", self.dma.recvchannel)):
            while not ch.idle:
                if time.perf_counter() > deadline:
                    raise DmaTimeout(f"DMA {name} still busy after {timeout * 1000:.0f} ms")
        return [int(v) / (1 << OUT_FRAC) for v in self.outb]
