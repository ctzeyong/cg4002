import numpy as np
from pynq import Overlay, allocate

ol  = Overlay('dma_loopback.bit')
dma = ol.axi_dma_0

n    = 6
inb  = allocate(shape=(n,), dtype=np.float32)
outb = allocate(shape=(n,), dtype=np.float32)
inb[:] = [1, 2, 4, 5.5, 1000, -10]

dma.sendchannel.transfer(inb)
dma.recvchannel.transfer(outb)
dma.sendchannel.wait()
dma.recvchannel.wait()

print("in :", inb)
print("out:", outb)
print("match:", np.allclose(inb, outb))   # True — floats + negatives, zero packing
