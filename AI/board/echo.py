import time
import numpy as np
from pynq import Overlay, allocate

ol  = Overlay("echo.xsa"); dma = ol.axi_dma_0; cnn = ol.cnn_axi_0
inb  = allocate(shape=(600,), dtype=np.int16)
outb = allocate(shape=(7,),   dtype=np.int16)

def dma_wait(ch, name, timeout=3.0):
    """Poll idle with a timeout so a stall never hangs the board."""
    t0 = time.time(); time.sleep(0.01)
    while not ch.idle:
        if time.time() - t0 > timeout:
            print(f"  [TIMEOUT] {name} after {timeout}s -- stopping channel", flush=True)
            try:    ch.stop()
            except Exception as e: print(f"  (stop failed: {e})")
            return False
    return True

inb[:] = np.arange(600, dtype=np.int16); inb.flush()
cnn.register_map.CTRL.AP_START = 1
dma.recvchannel.transfer(outb)
dma.sendchannel.transfer(inb)
ok_s = dma_wait(dma.sendchannel, "SEND")
ok_r = dma_wait(dma.recvchannel, "RECV")

if ok_s and ok_r:
    outb.invalidate()
    exp = [0, 100, 199, 300, 400, 500, 599]
    print("got:     ", list(outb))
    print("expected:", exp)
    print("MATCH" if list(outb) == exp else "MISMATCH")
else:
    print(f"DMA stalled (send={ok_s}, recv={ok_r}) -- output not read.")
    print("Re-run from a fresh Overlay(...) before the next test.")
