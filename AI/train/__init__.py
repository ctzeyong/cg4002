"""train: training and evaluation for the gesture QCNN.

Modules, each with one job:
  config    - all tunables as dataclasses (an experiment composes a RunConfig and nothing else)
  model     - THE architecture (build_model) + quantizers, owned here, no external imports
  augment   - on-the-fly augmentation: signal augs + window synthesis + feature channels
  data      - dataset loading, decode, label map, stratified split, windowed sequences
  train     - one reusable train_eval(RunConfig) -> Result
  evaluate  - per-class accuracy, confusion matrix, confidence

Experiments (exp_*.py) just compose a RunConfig and call train_eval; run them with
`python -m train.exp_new_vocab` from the `AI/` directory. Datasets come from
`data_collection.export_dataset` (AI/data_collection/datasets/).
"""
import os

# qkeras needs the legacy Keras; set before any TensorFlow import anywhere in the package.
os.environ.setdefault("TF_USE_LEGACY_KERAS", "1")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
