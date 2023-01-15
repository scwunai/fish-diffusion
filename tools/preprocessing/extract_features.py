import numpy as np
import librosa
import torch
from tqdm import tqdm
from fish_diffusion.utils.pitch import get_pitch_parselmouth, get_pitch_crepe
from fish_diffusion.utils.audio import get_mel_from_audio
from fish_diffusion.utils.tensor import repeat_expand_2d
from fish_diffusion.feature_extractors import FEATURE_EXTRACTORS
from multiprocessing import Lock, Value
from mmengine import Config
from pathlib import Path
import torchcrepe
from concurrent.futures import ProcessPoolExecutor, as_completed
from fish_audio_preprocess.utils.file import list_files
import argparse
import multiprocessing as mp


text_features_extractor = None


def init(worker_id: Value, lock: Lock, config):
    global text_features_extractor

    with lock:
        current_id = worker_id.value
        worker_id.value += 1

    device = torch.device(
        f"cuda:{current_id % torch.cuda.device_count()}"
        if torch.cuda.is_available()
        else "cpu"
    )

    text_features_extractor = FEATURE_EXTRACTORS.build(
        config.preprocessing.text_features_extractor
    )
    text_features_extractor.to(device)

    if config.preprocessing.pitch_extractor == "crepe":
        torchcrepe.load.model(device, "full")


def process(config, audio_path: Path, override: bool = False):
    # Important for multiprocessing
    global text_features_extractor

    audio, sr = librosa.load(str(audio_path), sr=None, mono=True)
    # audio: (1, T)

    # Obtain mel spectrogram
    mel_path = audio_path.parent / f"{audio_path.name}.mel.npy"

    if mel_path.exists() is False or override:
        audio_44k = librosa.resample(audio, orig_sr=sr, target_sr=44100)
        audio_44k = torch.from_numpy(audio_44k).unsqueeze(0)
        mel = get_mel_from_audio(audio_44k, 44100)
        np.save(mel_path, mel.cpu().numpy())
    else:
        mel = np.load(mel_path)

    # Move audio to appropriate device
    audio = torch.from_numpy(audio).unsqueeze(0).to(text_features_extractor.device)

    # Extract text features
    text_features_path = audio_path.parent / f"{audio_path.name}.text_features.npy"

    if text_features_path.exists() is False or override:
        text_features = text_features_extractor(audio, sr)[0]
        text_features = repeat_expand_2d(text_features, mel.shape[-1])
        np.save(text_features_path, text_features.cpu().numpy())

    # Extract f0
    f0_path = audio_path.parent / f"{audio_path.name}.f0.npy"

    if f0_path.exists() is False or override:
        if config.preprocessing.pitch_extractor == "crepe":
            f0 = get_pitch_crepe(audio, sr, pad_to=mel.shape[-1])
        elif config.preprocessing.pitch_extractor == "parselmouth":
            f0 = get_pitch_parselmouth(audio, sr, pad_to=mel.shape[-1])
        else:
            raise ValueError(
                f"Unknown pitch extractor: {config.preprocessing.pitch_extractor}"
            )

        np.save(f0_path, f0.cpu().numpy())


def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument("--config", type=str, required=True)
    parser.add_argument("--path", type=str, required=True)
    parser.add_argument("--override", action="store_true")
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--num-workers", type=int, default=4)

    return parser.parse_args()


if __name__ == "__main__":
    mp.set_start_method("spawn")

    args = parse_args()

    if args.clean:
        print("Cleaning...")

        files = list_files(args.path, {".npy"}, recursive=True, sort=True)
        for f in files:
            f.unlink()

        print("Done!")

    config = Config.fromfile(args.config)
    files = list_files(args.path, {".wav"}, recursive=True, sort=True)

    print(f"Found {len(files)} files, processing...")

    worker_id = Value("i", 0)
    lock = Lock()

    with ProcessPoolExecutor(
        max_workers=args.num_workers,
        initializer=init,
        initargs=(worker_id, lock, config),
    ) as executor:
        futures = [
            executor.submit(process, config, audio_path, args.override)
            for audio_path in files
        ]

        for i in tqdm(as_completed(futures), total=len(futures)):
            assert i.exception() is None, i.exception()

    print("Done!")
