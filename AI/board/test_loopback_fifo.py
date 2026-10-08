from pynq import Overlay, MMIO
import time

ol   = Overlay("design_1.bit")
ctrl = MMIO(0xA0000000, 0x10000)   # S_AXI      (control/status), or C_BASEADDR
data = MMIO(0xA0010000, 0x10000)   # S_AXI_FULL (data path), or C_AXI4_BASEADDR

# Control-window offsets (PG080)
ISR, TDFR, TDFV, TLR = 0x00, 0x08, 0x0C, 0x14
RDFR, RDFO, RLR      = 0x18, 0x1C, 0x24
# Data-window offsets (AXI4 mode)
TDFD = 0x0000     # write port
RDFD = 0x1000     # read port

ctrl.write(TDFR, 0xA5)          # reset TX FIFO
ctrl.write(RDFR, 0xA5)          # reset RX FIFO
ctrl.write(ISR, 0xFFFFFFFF)     # clear status

msg = [1, 2, 4, 5.5, 1000, -10]
for w in msg:
    data.write(TDFD, w)         # push words into TX
ctrl.write(TLR, len(msg) * 4)   # indicate no of bytes transmitted in TLR -> triggers stream

# Wait for data to loop back, with a timeout (never spin forever)
got = 0
for _ in range(1000):
    got = ctrl.read(RDFO) & 0x7FFFFFFF
    if got >= len(msg):
        break
    time.sleep(0.001)

if got == 0:
    print("No data returned - check the loopback wiring / TLR.")
else:
    rlen = ctrl.read(RLR)                       # bytes in packet
    out  = [data.read(RDFD) for _ in range(got)]   # read ONLY what's available
    print("sent:", msg, "got:", out, "match:", msg == out)
