"""Pack a trained model's data into one zip that reproduces its accuracy in a fresh clone,
without the raw recordings (they are git-ignored).

    python -m data_collection.pack_dataset --data mage_noise_robust --tag mage_noise_robust \
        --model train/results/models/mage_noise_robust_s2.h5 \
        --test-windows train/results/test_windows_mage_noise_robust.npz

Paths inside the zip are relative to AI/, so extracting it into AI/ puts every file where the
code looks for it:

    data_collection/datasets/<data>/                the exported gestures (export_dataset output)
    data_collection/datasets/background_v2_*.npz    the background cache for --sessions: the
                                                    unlabelled recording time that the noise class
                                                    and the slide_bg test are cut from
    train/results/models/<model>.h5                 the trained model (--model)
    train/results/run_<tag>.json                    its accuracy as recorded at training time
    train/results/test_windows_<tag>.npz            the windows the HLS check used (--test-windows)
    ultra96/gesture_model.xsa                       the deployed overlay (--xsa)
    REPRODUCE.txt                                   the commands that check all of it

--sessions must be the recordings the model was trained with (the training run's --sessions;
the default for the deployed model), so the background is the one it was scored on. The zip is
written to data_collection/datasets/ unless --out says otherwise.
"""
import argparse
import os
import zipfile

from train.config import DATA_ROOT, SESSIONS_DIR, DataConfig
from train.data import background_cache_path, load_background

AI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

REPRODUCE = """\
Reproduce {tag}

Extract this zip into the repo's AI/ folder (it adds files, it replaces none of the tracked
ones except ultra96/gesture_model.xsa, which is the same file). Build the venv as in
AI/README.md (Setup), then from AI/:

1. The model's accuracy, from the exported gestures + the background in this zip:

   .venv\\Scripts\\python -m train.export_test_windows --data {data} --noise ^
       --model {model} --out train/results/test_windows_check.npz

   "acc (as trained)" for jitter / slide_bg / noise equals train/results/run_{tag}.json
   (jitter, slide_bg, 1 - false_accept). {expected}

2. The converted model (what gesture_model.xsa runs), in WSL as in AI/hls4ml/README.md:

   python /mnt/c/<repo>/AI/hls4ml/convert.py --model {model_name} --npz <test windows>.npz --out ~/check

   with either test_windows_check.npz from step 1 or {test_windows}. Output (3) gives
   the Keras and HLS accuracy per test set; they agree to within a window or two.

3. Retraining from scratch (results close to, not necessarily identical with, step 1):

   .venv\\Scripts\\python -m train.exp_new_vocab --noise --data {data} --tag {tag}_retrain
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, help="dataset folder under DATA_ROOT")
    ap.add_argument("--tag", required=True, help="training run tag (for run_<tag>.json)")
    ap.add_argument("--model", required=True, help=".h5, relative to AI/")
    ap.add_argument("--test-windows", help="export_test_windows .npz, relative to AI/")
    ap.add_argument("--xsa", default="ultra96/gesture_model.xsa", help="overlay, relative to AI/ ('' to leave out)")
    ap.add_argument("--sessions", nargs="+", default=[SESSIONS_DIR],
                    help="the recordings the model was trained with (default SESSIONS_DIR)")
    ap.add_argument("--out", help="zip path (default data_collection/datasets/<tag>_release.zip)")
    a = ap.parse_args()

    data_cfg = DataConfig(dir=a.data, sessions_dir=a.sessions)
    bg = background_cache_path(data_cfg)
    if not os.path.exists(bg):                       # build it from the recordings if needed
        if load_background(data_cfg) is None:
            raise SystemExit(f"no background: no cache {bg} and no reviewed recordings in {a.sessions}")

    files = []                                       # (path on disk, path in the zip)
    data_dir = data_cfg.path()
    for root, _, names in os.walk(data_dir):
        for n in sorted(names):
            p = os.path.join(root, n)
            files.append((p, os.path.relpath(p, AI_ROOT)))
    files.append((bg, os.path.relpath(bg, AI_ROOT)))
    run_json = os.path.join("train", "results", f"run_{a.tag}.json")
    extras = [a.model, run_json] + ([a.test_windows] if a.test_windows else []) + ([a.xsa] if a.xsa else [])
    for rel in extras:
        p = os.path.join(AI_ROOT, rel)
        if not os.path.exists(p):
            raise SystemExit(f"missing {p}")
        files.append((p, os.path.relpath(p, AI_ROOT)))
    for p, arc in files:
        if arc.startswith(".."):
            raise SystemExit(f"{p} is outside AI/; DATA_ROOT must be the default for a release zip")

    import json
    run = json.load(open(os.path.join(AI_ROOT, run_json)))
    expected = "For this run: jitter {:.4f}, slide_bg {:.4f}, noise {:.4f}.".format(
        run["jitter"][0], run["slide_bg"][0], 1 - run["false_accept"][0])
    text = REPRODUCE.format(tag=a.tag, data=a.data, model=a.model.replace("\\", "/"),
                            model_name=os.path.basename(a.model), expected=expected,
                            test_windows=(a.test_windows or "<none packed>").replace("\\", "/"))

    out = a.out or os.path.join(DATA_ROOT, f"{a.tag}_release.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p, arc in files:
            z.write(p, arc.replace("\\", "/"))
        z.writestr("REPRODUCE.txt", text)
    print(f"{out}: {len(files) + 1} files, {os.path.getsize(out) / 1e6:.1f} MB")
    print(f"  dataset    {os.path.relpath(data_dir, AI_ROOT)} ({len(files) - len(extras) - 1} files)")
    print(f"  background {os.path.relpath(bg, AI_ROOT)}")
    for rel in extras:
        print(f"  {rel}")


if __name__ == "__main__":
    main()
