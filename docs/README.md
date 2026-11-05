# Project documentation

## Subsystem documentation

Subsystem guides explain how one subsystem is designed, which files belong to it, and how to
run it independently.

| Subsystem | Documentation |
|---|---|
| Communications | [Configuration B pipeline and Configuration A phone-hotspot alternative](subsystem/comms/README.md) |

## Integration documentation

Cross-subsystem contracts, full-system setup, and integration runbooks belong in
[`integration/`](integration/). This keeps shared interfaces separate from implementation
details owned by one subsystem.

| Integration | Documentation |
|---|---|
| Full pipeline | [Merged Hardware -> Comms -> AI -> Visualiser flow and Configuration B runbook](integration/full-pipeline.md) |
| Hardware ↔ Comms | [Single-MPU6050 interface, ownership, and change contract](integration/hardware-comms.md) |
| AI ↔ Comms | [FPGA classifier in the board client](integration/ai-comms.md) |
| Game engine on the Ultra96 | [Who owns each part of the engine, and what each side may change](integration/engine-ownership.md) |

When adding documentation:

- place subsystem-specific material under `subsystem/<name>/`;
- place shared workflows and interface contracts under `integration/`; and
- add the new document to this index.
