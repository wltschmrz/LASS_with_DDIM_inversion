import os
import sys
import re
from typing import Dict, List
import traceback

proj_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
src_dir = os.path.join(proj_dir, 'src')
sys.path.extend([proj_dir, src_dir])

import matplotlib.pyplot as plt

import csv
import pandas as pd
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from tqdm import tqdm
import pathlib
import librosa
import yaml
os.environ["HF_HOME"] = os.path.expanduser("~/.cache/huggingface")
# import lightning.pytorch as pl
# from models.clap_encoder import CLAP_Encoder

import soundfile as sf
from src.audioldm import AudioLDM
from src.audioldm2 import AudioLDM2

from src.dataprocessor import AudioDataProcessor
from src.sep_editing import inference

import torchaudio

def load_audio_torch(source_path, sampling_rate, mono=True):
    waveform, sr = torchaudio.load(source_path, normalize=True)  # librosa처럼 float32 [-1, 1]로 로드
    waveform = waveform.mean(dim=0) if (waveform.shape[0] > 1) and mono else waveform  # mono 변환
    waveform = torchaudio.functional.resample(waveform, sr, sampling_rate) if sr != sampling_rate else waveform
    return waveform.numpy().squeeze(0), sampling_rate

def calculate_sdr(ref: np.ndarray, est: np.ndarray, eps=1e-10) -> float:
    r"""Calculate SDR between reference and estimation.
    Args:
        ref (np.ndarray), reference signal
        est (np.ndarray), estimated signal
    """
    reference = ref
    noise = est - reference
    numerator = np.clip(a=np.mean(reference ** 2), a_min=eps, a_max=None)
    denominator = np.clip(a=np.mean(noise ** 2), a_min=eps, a_max=None)
    sdr = 10. * np.log10(numerator / denominator)
    return sdr

def calculate_sisdr(ref, est):
    r"""Calculate SDR between reference and estimation.
    Args:
        ref (np.ndarray), reference signal
        est (np.ndarray), estimated signal
    """
    eps = np.finfo(ref.dtype).eps
    reference = ref.copy()
    estimate = est.copy()
    reference = reference.reshape(reference.size, 1)
    estimate = estimate.reshape(estimate.size, 1)
    Rss = np.dot(reference.T, reference)
    # get the scaling factor for clean sources
    a = (eps + np.dot(reference.T, estimate)) / (Rss + eps)
    e_true = a * reference
    e_res = estimate - e_true
    Sss = (e_true**2).sum()
    Snn = (e_res**2).sum()
    sisdr = 10 * np.log10((eps+ Sss)/(eps + Snn))
    return sisdr 

def get_mean_sdr_from_dict(sdris_dict):
    mean_sdr = np.nanmean(list(sdris_dict.values()))
    return mean_sdr

def parse_yaml(config_yaml: str) -> Dict:
    r"""Parse yaml file.
    Args:
        config_yaml (str): config yaml path
    Returns:
        yaml_dict (Dict): parsed yaml file
    """
    with open(config_yaml, "r") as fr:
        return yaml.load(fr, Loader=yaml.FullLoader)


class AudioCapsEvaluator:
    def __init__(self, query='caption', sampling_rate=32000) -> None:
        r"""AudioCaps evaluator.
        Args:
            query (str): type of query, 'caption' or 'labels'
        Returns:
            None
        """
        self.query = query
        self.sampling_rate = sampling_rate
        with open(f'src/evaluation/metadata/audiocaps_eval_with_mixed_caption.csv') as csv_file:
            csv_reader = csv.reader(csv_file, delimiter=',')
            eval_list = [row for row in csv_reader][1:]
        self.eval_list = eval_list
        self.audio_dir = f'data/audiocaps'

    def __call__(self, pl_model, config) -> Dict:
        r"""Evalute."""
        print(f'Evaluation on AudioCaps with [{self.query}] queries.')
        
        processor, audioldm = pl_model
        device = audioldm.device

        for param in audioldm.parameters():
            param.requires_grad = False

        sisdrs_list = []
        sdris_list = []
        samples = config['samples']
        
        for eval_data in tqdm(self.eval_list[:samples]):

            # idx, caption, labels, _, _ = eval_data
            idx, caption, labels, _, _, mixed_caption = eval_data

            source_path = os.path.join(self.audio_dir, f'segment-{idx}.wav')
            mixture_path = os.path.join(self.audio_dir, f'mixture-{idx}.wav')
                            
            if self.query == 'caption':
                text = [caption]
            elif self.query == 'labels':
                text = [labels]
            mixed_text = [mixed_caption]

            config['mixed_text'] = mixed_text[0]
            config['text'] = text[0]

            sisdr_li, sdri_li = inference(audioldm, processor,
                      target_path=source_path,
                      mixed_path=mixture_path,
                      config=config)

            sisdr_li.append(caption)
            sdri_li.append(caption)

            sisdrs_list.append(sisdr_li)
            sdris_list.append(sdri_li)
            
        sisdrs_array = np.array(sisdrs_list)  # (samples, iterations)
        sdris_array = np.array(sdris_list)    # (samples, iterations)

        # # 각 iteration 별 평균을 계산 (samples에 대해 평균을 구함)
        # mean_sisdr = np.mean(sisdrs_array, axis=0)  # (iterations,)
        # mean_sdri = np.mean(sdris_array, axis=0)

        # # mean_sisdr = np.mean(sisdrs_list)
        # # mean_sdri = np.mean(sdris_list)
        
        # return mean_sisdr, mean_sdri

        return sisdrs_array, sdris_array

if __name__ == "__main__":
    def clean_wav_filenames(dir_path):
        if not os.path.exists(dir_path):
            return
        for filename in os.listdir(dir_path):
            if filename.endswith(".wav"):
                file_path = os.path.join(dir_path, filename)
                os.remove(file_path)
            elif filename.endswith(".png"):
                file_path = os.path.join(dir_path, filename)
                os.remove(file_path)
    def ensure_folder_exists(folder_path):
        os.makedirs(folder_path, exist_ok=True)

    # Ensure the folders exist before calling clean_wav_filenames
    folders = ["./test/batch_samples", "./test/plot", "./test/result"]

    for folder in folders:
        ensure_folder_exists(folder)


    clean_wav_filenames("./test/batch_samples")
    clean_wav_filenames("./test/plot")
    clean_wav_filenames("./test/result")

    eval = AudioCapsEvaluator(query='caption', sampling_rate=16000)
    
    audioldm = AudioLDM('cuda:0')
    device = audioldm.device
    processor = AudioDataProcessor(device=device)

    # for i in range(4, 5):
    config = {
        'num_epochs': 400,  # 50?
        'batchsize': 32,
        'strength': 0.7,  # 0.6,
        'learning_rate': 0.01,
        'iteration': 5,
        'samples': 100,  # number of samples to evaluate
        'steps': 25,  # 50
    }

    # mean_sisdr, mean_sdri = eval((processor, audioldm), config)
    sisdr_array, sdri_array = eval((processor, audioldm), config)

    def format_number(num):
        num = float(num)  # 문자열이 아니라 숫자로 변환
        formatted = f"{num:.4f}"
        return formatted if num < 0 else f" {formatted}"

    vec_format = np.vectorize(format_number)
    formatted_sisdrs = vec_format(sisdr_array[:, :5].astype(float))
    formatted_sdris = vec_format(sdri_array[:, :5].astype(float))

    combined_data = np.core.defchararray.add(formatted_sisdrs, ' / ')
    combined_data = np.core.defchararray.add(combined_data, formatted_sdris)

    df = pd.DataFrame(combined_data)
    df['caption'] = sisdr_array[:, 5].astype(str)  # caption 데이터는 sisdrs_array의 마지막 열 사용

    df.columns = [f'iter {i+1}' for i in range(5)] + ['caption']
    df.to_csv('sisdr_sdri_results_null.csv', index=False)
    print("CSV 저장 완료: sisdr_sdri_results.csv")


    # print("========== SI-SDR  |  SDRi ===========")
    # strength_ = config['strength']
    # for epoch, (mean_sisdr, mean_sdri) in enumerate(zip(mean_sisdr, mean_sdri)):
    #     if not ((epoch+1) % 100):
    #         print(f">> {strength_},{epoch+1}::{mean_sisdr:.4f} / {mean_sdri:.4f}")



    # import matplotlib.pyplot as plt
    # import scipy.io.wavfile as wav
    # import librosa.display as ld  

    # def plot_wav_mel(wav_paths, save_path="./test/waveform_mel.png"):
    #     fig, axes = plt.subplots(2, len(wav_paths), figsize=(4 * len(wav_paths), 6))

    #     for i, wav_path in enumerate(wav_paths):
    #         sr, data = wav.read(wav_path)
    #         time = np.linspace(0, len(data) / sr, num=len(data))
            
    #         # Waveform
    #         axes[0, i].plot(time, data, lw=0.5)
    #         axes[0, i].set_title(f"Waveform {i+1}")
    #         axes[0, i].set_xlabel("Time (s)")
    #         axes[0, i].set_ylabel("Amplitude")

    #         # Mel Spectrogram
    #         y, sr = librosa.load(wav_path, sr=None)
    #         mel_spec = librosa.feature.melspectrogram(y=y, sr=sr, n_mels=128)
    #         mel_spec_db = librosa.power_to_db(mel_spec, ref=np.max)
    #         ld.specshow(mel_spec_db, sr=sr, x_axis="time", y_axis="mel", ax=axes[1, i])
    #         axes[1, i].set_title(f"Mel Spectrogram {i+1}")

    #     plt.tight_layout()
    #     plt.savefig(save_path, dpi=300, bbox_inches='tight')
    #     plt.close()

    # wavs = ['./test/origin.wav',
    #         './test/time_align_shit.wav',
    #         './test/orig.wav',
    #         './test/noised.wav',]

    # plot_wav_mel(wavs)


    # audioldm = AudioLDM('cuda:1')
    # device = audioldm.device
    # processor = AudioDataProcessor(device=device)
    # target_path = './samples/A_cat_meowing.wav'
    # mixed_path = './samples/a_cat_n_stepping_wood.wav'

    # config = {
    #     'num_epochs': 50,
    #     'batchsize': 1,
    #     'strength': 0.6,
    #     'learning_rate': 0.01,
    #     'iteration': 5,
    #     'steps': 25,  # 50
    #     'text': ['A cat meowing'],
    #     'mixed_text': ['A cat meowing and footstep on the wooden floor'],
    # }

    # what, iss = inference(audioldm, processor, target_path, mixed_path, config)
    # print(what, iss)
