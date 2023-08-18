import os
import random
import subprocess
import tempfile
from typing import Literal
import numpy as np
import torch
from scipy import linalg
from pathlib import Path
from hypy_utils import write
from hypy_utils.tqdm_utils import tq, tmap, pmap
from hypy_utils.nlp_utils import substr_between
from hypy_utils.logging_utils import setup_logger

from .model_loader import ModelLoader
from .utils import *

log = setup_logger()


def calc_embd_statistics(embd_lst: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """
    Calculate the mean and covariance matrix of a list of embeddings.
    """
    return np.mean(embd_lst, axis=0), np.cov(embd_lst, rowvar=False)


def calc_frechet_distance(mu1, cov1, mu2, cov2, eps=1e-6):
    """
    Adapted from: https://github.com/mseitzer/pytorch-fid/blob/master/src/pytorch_fid/fid_score.py
    
    Numpy implementation of the Frechet Distance.
    The Frechet distance between two multivariate Gaussians X_1 ~ N(mu_1, C_1)
    and X_2 ~ N(mu_2, C_2) is
            d^2 = ||mu_1 - mu_2||^2 + Tr(C_1 + C_2 - 2*sqrt(C_1*C_2)).
    Stable version by Dougal J. Sutherland.
    Params:
    -- mu1   : Numpy array containing the activations of a layer of the
            inception net (like returned by the function 'get_predictions')
            for generated samples.
    -- mu2   : The sample mean over activations, precalculated on an
            representative data set.
    -- cov1: The covariance matrix over activations for generated samples.
    -- cov2: The covariance matrix over activations, precalculated on an
            representative data set.
    Returns:
    --   : The Frechet Distance.
    """
    mu1 = np.atleast_1d(mu1)
    mu2 = np.atleast_1d(mu2)

    cov1 = np.atleast_2d(cov1)
    cov2 = np.atleast_2d(cov2)

    assert mu1.shape == mu2.shape, \
        f'Training and test mean vectors have different lengths ({mu1.shape} vs {mu2.shape})'
    assert cov1.shape == cov2.shape, \
        f'Training and test covariances have different dimensions ({cov1.shape} vs {cov2.shape})'

    diff = mu1 - mu2

    # Product might be almost singular
    covmean, _ = linalg.sqrtm(cov1.dot(cov2), disp=False)
    if not np.isfinite(covmean).all():
        msg = ('fid calculation produces singular product; '
            'adding %s to diagonal of cov estimates') % eps
        log.info(msg)
        offset = np.eye(cov1.shape[0]) * eps
        covmean = linalg.sqrtm((cov1 + offset).dot(cov2 + offset))

    # Numerical error might give slight imaginary component
    if np.iscomplexobj(covmean):
        if not np.allclose(np.diagonal(covmean).imag, 0, atol=1e-3):
            m = np.max(np.abs(covmean.imag))
            raise ValueError('Imaginary component {}'.format(m))
        covmean = covmean.real

    tr_covmean = np.trace(covmean)

    return (diff.dot(diff) + np.trace(cov1)
            + np.trace(cov2) - 2 * tr_covmean)


class FrechetAudioDistance:
    device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')
    loaded = False

    def __init__(self, ml: ModelLoader, audio_load_worker=8, sox_path="sox", load_model=True):
        self.ml = ml
        self.audio_load_worker = audio_load_worker
        self.sox_path = sox_path
        self.sox_formats = find_sox_formats(sox_path)

        if load_model:
            self.ml.load_model()
            self.loaded = True

        # Disable gradient calculation because we're not training
        torch.autograd.set_grad_enabled(False)

    def load_audio(self, f: str | Path):
        f = Path(f)

        # Create a directory for storing normalized audio files
        cache_dir = f.parent / "convert" / str(self.ml.sr)
        new = (cache_dir / f.name).with_suffix(".wav")

        if not new.exists():
            sox_args = ['-r', str(self.ml.sr), '-c', '1', '-b', '16']
            cache_dir.mkdir(parents=True, exist_ok=True)

            # ffmpeg has bad resampling compared to SoX
            # SoX has bad format support compared to ffmpeg
            # If the file format is not supported by SoX, use ffmpeg to convert it to wav

            if f.suffix[1:] not in self.sox_formats:
                # Use ffmpeg for format conversion and then pipe to sox for resampling
                with tempfile.TemporaryDirectory() as tmp:
                    tmp = Path(tmp) / 'temp.wav'

                    # Open ffmpeg process for format conversion
                    subprocess.run([
                        "/usr/bin/ffmpeg", 
                        "-hide_banner", "-loglevel", "error", 
                        "-i", f, tmp])
                    
                    # Open sox process for resampling, taking input from ffmpeg's output
                    subprocess.run([self.sox_path, tmp, *sox_args, new])
                    
            else:
                # Use sox for resampling
                subprocess.run([self.sox_path, f, *sox_args, new])

        return self.ml.load_wav(new)
    
    def get_cache_embedding_path(self, audio_dir: str | Path) -> Path:
        """
        Get the path to the cached embedding npy file for an audio file.
        """
        return Path(audio_dir).parent / "embeddings" / self.ml.name / audio_dir.with_suffix(".npy").name

    def cache_embedding_file(self, audio_dir: str | Path):
        """
        Compute embedding for an audio file and cache it to a file.
        """
        cache = self.get_cache_embedding_path(audio_dir)

        if cache.exists():
            return

        # Load file
        wav_data = self.load_audio(audio_dir)
        
        # Compute embedding
        embd = self.ml.get_embedding(wav_data)
        
        # Save embedding
        cache.parent.mkdir(parents=True, exist_ok=True)
        np.save(cache, embd)

    def read_embedding_file(self, audio_dir: str | Path):
        """
        Read embedding from a cached file.
        """
        cache = self.get_cache_embedding_path(audio_dir)
        assert cache.exists(), f"Embedding file {cache} does not exist, please run cache_embedding_file first."
        return np.load(cache)
    
    def load_embeddings(self, dir: str | Path, max_count: int = -1, concat: bool = True):
        """
        Load embeddings for all audio files in a directory.
        """
        dir = Path(dir)

        # List valid audio files
        files = [dir / f for f in os.listdir(dir)]
        files = [f for f in files if f.is_file()]
        log.info(f"Loading {len(files)} audio files from {dir}...")

        return self._load_embeddings(files, max_count=max_count, concat=concat)

    def _load_embeddings(self, files: list[Path], max_count: int = -1, concat: bool = True):
        """
        Load embeddings for a list of audio files.
        """
        # Load embeddings
        if max_count == -1:
            embd_lst = tmap(self.read_embedding_file, files, desc="Loading audio files...", max_workers=self.audio_load_worker)
        else:
            total_len = 0
            embd_lst = []
            for f in tq(files, "Loading files"):
                embd_lst.append(self.read_embedding_file(f))
                total_len += embd_lst[-1].shape[0]
                if total_len > max_count:
                    break
        
        # Concatenate embeddings if needed
        if concat:
            return np.concatenate(embd_lst, axis=0)
        else:
            return embd_lst, files
    
    def load_stats(self, dir: str | Path):
        """
        Load embedding statistics from a directory.
        """
        dir = Path(dir)
        cache_dir = dir / "stats" / self.ml.name
        emb_dir = dir / "embeddings" / self.ml.name
        if cache_dir.exists():
            log.info(f"Embedding statistics is already cached for {dir}, loading...")
            mu = np.load(cache_dir / "mu.npy")
            cov = np.load(cache_dir / "cov.npy")
            return mu, cov

        log.info(f"Loading embedding files from {dir}...")
        
        mu, cov = calculate_embd_statistics_online(list(emb_dir.glob("*.npy")))
        log.info("> Embeddings statistics calculated.")

        # Save statistics
        cache_dir.mkdir(parents=True, exist_ok=True)
        np.save(cache_dir / "mu.npy", mu)
        np.save(cache_dir / "cov.npy", cov)
        
        return mu, cov

    def score(self, background_dir: Path | str, eval_dir: Path | str):
        """
        Calculate a single FAD score between a background and an eval set.
        """
        mu_bg, cov_bg = self.load_stats(background_dir)
        mu_eval, cov_eval = self.load_stats(eval_dir)

        return calc_frechet_distance(mu_bg, cov_bg, mu_eval, cov_eval)

    def fadinf(self, baseline_dir: Path, eval_files: list[Path], steps: int = 25, min_n = 500):
        """
        Calculate FAD for different n (number of samples) and compute FAD-inf.

        :param baseline_dir: directory with baseline audio files
        :param eval_files: list of eval audio files
        :param steps: number of steps to use
        :param min_n: minimum n to use
        """
        log.info(f"Calculating FAD-inf for {self.ml.name}...")
        # 1. Load background embeddings
        mu_base, cov_base = self.load_stats(baseline_dir)
        # If all of the embedding files end in .npy, we can load them directly
        if all([f.suffix == '.npy' for f in eval_files]):
            embeds = [np.load(f) for f in eval_files]
            embeds = np.concatenate(embeds, axis=0)
        else:
            embeds = self._load_embeddings(eval_files, concat=True)
        
        # Calculate maximum n
        max_n = len(embeds)

        # Generate list of ns to use
        ns = [int(n) for n in np.linspace(min_n, max_n, steps)]
        
        results = []
        for n in tq(ns, desc="Calculating FAD-inf"):
            # Select n feature frames randomly (with replacement)
            indices = np.random.choice(embeds.shape[0], size=n, replace=True)
            embds_eval = embeds[indices]
            
            mu_eval, cov_eval = calc_embd_statistics(embds_eval)
            fad_score = calc_frechet_distance(mu_base, cov_base, mu_eval, cov_eval)

            # Add to results
            results.append([n, fad_score])

        # Compute FAD-inf based on linear regression of 1/n
        ys = np.array(results)
        xs = 1 / np.array(ns)
        slope, intercept = np.polyfit(xs, ys[:, 1], 1)

        # Since intercept is the FAD-inf, we can just return it
        return intercept, slope
    
    def score_individual(self, background_dir: PathLike, eval_dir: PathLike, csv_name: str):
        """
        Calculate the FAD score for each individual file in eval_dir and write the results to a csv file.

        
        """
        csv = Path('data') / f'fad-individual' / self.ml.name / csv_name
        if csv.exists():
            log.info(f"CSV file {csv} already exists, exitting...")
            return

        # 1. Load background embeddings
        mu, cov = self.load_stats(background_dir)

        # 2. Define helper function for calculating z score
        def _find_z_helper(f):
            try:
                # Calculate FAD for individual songs
                embd = self.read_embedding_file(f)
                mu_eval, cov_eval = calc_embd_statistics(embd)
                return calc_frechet_distance(mu, cov, mu_eval, cov_eval)

            except Exception as e:
                log.error(f"An error occurred calculating individual FAD using model {self.ml.name} on file {f}")
                log.error(e)

        # 3. Calculate z score for each eval file
        _files = list(Path(eval_dir).glob("*.*"))
        scores = tmap(_find_z_helper, _files, desc=f"Calculating scores", max_workers=self.audio_load_worker)

        # 4. Write the sorted z scores to csv
        pairs = list(zip(_files, scores))
        pairs = [p for p in pairs if p[1] is not None]
        pairs = sorted(pairs, key=lambda x: np.abs(x[1]))
        write(csv, "\n".join([",".join([str(x).replace(',', '_') for x in row]) for row in pairs]))

        return csv
