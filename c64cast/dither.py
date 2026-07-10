"""Spatial dithering for pre-quantization color shaping.

Two families, selected by ``[color].dither``:

- **Ordered (Bayer 8×8 / blue noise 64×64)** — a fixed, position-deterministic
  threshold offset added to every pixel before nearest-palette quantization.
  Vectorized (one array op over the whole frame) and temporally stable (the
  same pixel position always gets the same offset), so it holds realtime
  frame rates without adding frame-to-frame shimmer. `bayer_offset` and
  `blue_noise_offset` are the two primitives (same contract — an additive
  (h, w) offset); callers add the result to the pixel array before
  quantizing. Bayer's regular 8×8 tiling has visible cross-hatch/grid
  structure at C64 resolution; the blue-noise tile (baked from a
  void-and-cluster mask — see `scripts/diags/gen_blue_noise.py`) has the same
  cost and stability properties with no periodic structure, so it's the
  better default wherever an ordered method applies (`config.
  resolve_dither_method`).
- **Error diffusion (Floyd-Steinberg / Atkinson)** — a sequential per-pixel
  scan that pushes each pixel's quantization error onto its yet-unvisited
  neighbors. Higher quality on static content (no competing candidate set
  reproduces gradients as well) but a Python-level loop — too slow for
  realtime video, and diffusing across frames independently makes each
  frame's error pattern independent of the last, which reads as shimmer on
  motion. `error_diffuse` is the primitive; callers run it once per
  candidate-set region (e.g. once per display cell) against that region's
  resolved palette subset.

Both primitives take an explicit BGR candidate set rather than the fixed
16-color C64 palette, so the same code dithers a global 16-color pass or a
per-cell subset (e.g. mhires' per-cell {bg0, c1, c2, c3}) identically.
"""

from __future__ import annotations

import base64

import numpy as np

DITHER_METHODS: tuple[str, ...] = ("none", "ordered", "blue_noise", "floyd-steinberg", "atkinson")

# 8x8 Bayer ordered-dither threshold matrix (index-value form, 0..63).
_BAYER_8X8 = np.array(
    [
        [0, 32, 8, 40, 2, 34, 10, 42],
        [48, 16, 56, 24, 50, 18, 58, 26],
        [12, 44, 4, 36, 14, 46, 6, 38],
        [60, 28, 52, 20, 62, 30, 54, 22],
        [3, 35, 11, 43, 1, 33, 9, 41],
        [51, 19, 59, 27, 49, 17, 57, 25],
        [15, 47, 7, 39, 13, 45, 5, 37],
        [63, 31, 55, 23, 61, 29, 53, 21],
    ],
    dtype=np.float32,
)
# Normalized to a zero-mean -0.5..~0.48 range so `bayer_offset` can scale it
# by an arbitrary strength without a magic 64 divisor at every call site.
_BAYER_NORM = (_BAYER_8X8 / 64.0) - 0.5

# 64x64 blue-noise ordered-dither threshold matrix (index-value form,
# 0..4095), generated offline by the void-and-cluster algorithm — see
# scripts/diags/gen_blue_noise.py (size=64 sigma=1.9 seed=0). Baked as a
# base64 uint16 blob rather than a runtime generator call: generation isn't
# vectorizable (each rank depends on the previous swap), so it stays a
# build-time artifact, reproducible from the script, not a per-import cost.
_BLUE_NOISE_SIZE = 64
_BLUE_NOISE_B64 = (
    "eg47AKgF9wmgDS4Dfgc6AUYKDg20ACMJRQN7DT8IfAo0D04CsQXRDZkBOgblC1oP4wYuCyEATQZE"
    "BQwCVASuCzMBbgrVDFsEbgtdDusHrgHUDmwJnwfkBCwCaAp3A1MGmAl7DL0OOQaUAfANjQV7D1MA"
    "hAi5BbQHrAokD/IMjQNfDGMIYg/OA8kLRAbtCicPtgNmC6QFCwLrCcwEgQHfA2cH8wnLAIwPiQil"
    "BGcNJQnaA3kOzwwCD/oI+QmFDDAIuQM/D+sBbgmfAhYHHw1iBacDgADLDRUMMg8eCNQAmA9xCBsC"
    "fw2RB5EKiQlNBGQMVgrPDmkDSQ7VAEUFbQRbByULkgLiBBAH4wFJCXoEwQhgAjkILQ5XD4cMjQaX"
    "C5MOHQYUDf0EJwszA5cKJAKbAJMFNwh+AmoHbAPmDYkBpA4wBa4IaAbxDQIFYgDACt4IfA9jC84G"
    "bAHuA0sOFQfZC1UE9AowA+oEKg+3AkQINAEnB44CzQl0DNkGMwmwCyUBjgkgDqgKmAytDj4NWQBp"
    "Ba8N3AYXATwEpQfcCOIAsQ32AR0IJgz9AwgHqgzABzgOhAvMCVoBqQQQCwoGcADWBtMKbg32ACcK"
    "zw9CDA4DPgYTAoUEdQgbCgYGXQmnAj0NxQVmADEJCAy1AOEFWw4ZC8IEZA1KBhEEIQKuD7YNIwOL"
    "BWABEgjSAOYCogd2C7QPFwogAw8MiwqyAuUOcQXUCgQDGAk5AKwOlAkuAfYPYwb+AlcMdw9GDZQI"
    "GwzkD9wHcQKgC2ADYwcbBCgIsA2mCYEOcQzGAoANJACTDPYEpQE2CugNyg8XCO4GGw15A/MI4g+N"
    "AJQLtQjbCiMA1wdqBgcNQgTjD5QGLwoFBPgFvAG6DL4ENgkYBisAzAyaA7oJmgS2D58GjA2cAisF"
    "2QgTBFwKDwXeAAYH+AEuBB8D0gmzBMEMzgUqCdgLzgCHBXQBUgfzCkgF+w+/B6AKBQ/PCIAHWgaz"
    "A1QCSAsTCtwBJAydB3gFoQHfDrUM3wRUCqYO7wHDCysJGwVGDAgPmgkDCLAATQfaDZcPVgtOCAIH"
    "Dg7sC5oHnQF1C/4F2Qr4DV4AdQenDWIJ1gWWCkwO8QiWDbQBZw9IAGcOTAIQD4wKywwKBPsAJAl6"
    "A/wBlQsqBBcDcA48AVQMlQTwDvoAkAYoBLAJDQ5EA0AH4wVtAsIDkAhEB4gKYgPyDRIASgJiDZMD"
    "3g79CuoB+AMnBZYBGQ+qAFoChgUlCs0D0wwZCMgBBQw9DzkCgQO8Do8LBwBjBW4GbAoiB1QIBAvr"
    "BMEGVgP3B0kPdgYADJsO5AXwBr0A8wzFCmgFkgnDDb8IlQVxDUwPpAKvCiwIJA3bCVsPDAyXAPgO"
    "qgIkBlUIIQvlBuMIVQUqDFgGjwidAjUNawkwBngKXw14CNYO0ABqDygDvQmABCsLdwYjCNEMvwKE"
    "B30MEwHdDiYEAgM5DckJDw7/CMoBXQ0JCm0AgwjYDd8JLgV6D1kIpwsZAFkHDAPaCgwIZAAQBU4M"
    "RQFmBNYAyAZCCZQNvAQFASoNkA9IAaEEdA7SAsQJBwE5DiwK3AuxB10DawRpDO8CTwnlBGEHkQZz"
    "DnYFFg0PCaUABgrBBLUPqQN0CU4LMAzoASwGCwFdBJ4L3AXbAu0EHgtTBHQCMwyqB7UBuQKuBjcP"
    "CALZDN0Dvgt3DvcGAgmfD38L6w0YAwALqwVhDKQJeAvbA5UHZQq1C3cA/w93BCQHjgW7Dl8AvA8V"
    "C9sGIAHfDa4KNgwaAJ4IugIvAe8DCQ+6BbIBiwjgDVECywcLBcoIfg+XDF8HHADND1AMNw5xB2EB"
    "rA8MCbEDcw0mCvsD5gQCDjkKQgZkAXUJNwKJA1kGSQVwCNIBCwTUB0gObAajCF0FMwK2DNMF7gdA"
    "DdgKKgOzDFUB2ATSCO0BZg71B/oFMAIaBIENWQuDD3cKGwedDfYKDQzCBkUK/AVPDWMARg4OCpcD"
    "MgjKCj0CZwliCGUDnAYADYcKUQAiBsYOaAxhCfAHzwDrD6gEsgxzClINCgCYDiIKvAzaD04Afwo5"
    "A5MBCg5YDxEJcwOnAXkGPgkNAi8IRAuYBhwNAApgBYYDgwvnD9EJcgG6B9gFgAweAuwHFAOiDjkE"
    "uABVD0oDEQfrCnIFWwHmDsYN2gZbBZ4A6w6SC6AFxwRNDkILkwgcAakFaQuRAg8HeQifBfsOkgdK"
    "C64CRQftBSEJawIwDy8HFgzsCTMA9QatDfIOMQz1AxMOYA+TCcoDyAILDB8PcwC7DPQGzQgeBUED"
    "MQ57BHoJPwAoBSgJXQGZC6sMZAliBGUC9gtkBvACtgRICtkDTQ3DCRACxgj+ACUHPgNEAmsHdQ9y"
    "A1sNbg4ZDAgDqAEXBN8I1ARNAZsNwQuKBFEIKgVLDTsEwwL/CocEGgrXADQFbAcJAMsFxA1XB/QA"
    "WwhzCW8EsAKoDvAMkAAuCq0GugveD/kMHA5mB4kFKwi4AZUN8w+ACKUJ/AxYC7MBZQ/5C8UH5QIJ"
    "DlkMSg/QCZsECw1xCjQA1ghDBN8KnADmCfwNdwxsD8QKkgOlBrsAEQskAbEO0gfQC5oFPQhLAsUI"
    "mAt6CnwMYwKqDqsKKQVTDWAGhQqrAQELBQZGCDUPvwGtA2oIPwaWAqoKugPJDiYL+gSGBxkBrw5P"
    "AKwH7wgeBg4BjATlCmUGBwQBCLAB0guVDokGTQXGCe4BKwajDy0IuwbDBYMA+gexCdUOEA3mCLID"
    "GgaBCYAB1A/uDEcOCwaND+0CrgSZCG0BuAscBB0CCA5fD5MHBwzFA74NhAJVDMsKEQGmBOEJdgwG"
    "AlYGFgAhCg8DTwwjBKEFrA2UAokOLAfADJkPBAApCkENwQUSCVAB1AOlDAsOpAe9CwcFIw1FAvEL"
    "FgM7DvABcwW+AqoPRApAAvUNsgZOA3EANgfBA04BVw1RBgsK/QbWDy4J5QdeA0QAywQQCQMB1Qah"
    "CVwFeAdyDSUPnQDACKsPGQ0VCVoO7AXsBs0KQQmEA6kLLwWBClUDGglSBX8CUgvpBtwP6gKyClMI"
    "0wAvD0kDRAksAegDWwr2CIEEZwupB1oMBAdFAHMLiAywCAMF1glbDDgLVgmrBxIPmAO3DKwABwZT"
    "C3IMGQqmBQoNyw9eCzYE4w4DCSYDgAt9BQ0H3gKwB+sDXQs7AhgP3QzLAXQPGgiSAH4N6QFCCKEO"
    "zg2WA4EA2wRsDUkHYQtdAp4E9gaYDSgLsw4qB/UPqAxiBnwBGATPDawFlwTLDvIAtgpGDyACeAQp"
    "Ds0AzQsBAlAO0gToAuAOMgH6Bj0OKgIQA+MHCwCxDFgBhAb+DQME4AoiDNwEQwGCDfIHfAC6BP4J"
    "UgYzDmMEkAmXBs4LOQF7B6wJIwylCGIOIwGEBcUOfwwSCnsAAgZhCKMBRwUdAIoNigpgCQwPGQNI"
    "CHYH8QONDVUGCAheBY0KzgJNCJQFlQneCqsIiw2tCbwDcwjmCk4GUwrXDeMEZAI0CgAItgFxDmoA"
    "XQ+rCXsF9QvECDwDRwdGAR8LeAKLDMIP7APzBfoKQg/yAR8GOAoWBH8JcAapCOcCcg9zBOMMoALY"
    "CaEDkA56CAABuQvbDMUJLAusApsBIgmUDjIAvAYpDVcEzQ1pByAAqwZNAjwFxAuGD4kEnAGfDpgI"
    "5wXgCwYPBg1oCQMGfAhgCsMGwgImDoAK3w9MDS4MsgiSBZQHNwrYAP8MygJfBCIIqQyvAqAPJQ0U"
    "APML0QHDB5gKHw4YDHoHNQvXBcUC8QR0BgkCggDGDw8G/QyWCyUDOAzyD+EIQwsoAYkPRwwfBB0P"
    "xgduAWEAcA1oBy8MfAPACR4H7QBUAwwFiQJ6DKID6A7PAT0ENAbsAD8F4QMNDyUALAPSDvUE4AgA"
    "B/oNSwV3AYgLrQd9AxYF7wrcDa0FXAO/AIcGFgkpD9MBMQ3NB7wKyAPeDR8HGQUUCjcEhwc3AfoJ"
    "jwMpAkAGHgpGAykLDw0UBl8KoQxxCWEFhgKJAHgPMATqCqAHoQ9bC6gNJgExCAQNQAuXB1cJVwKj"
    "CtMG3Q1ICWIL0AFoDmoKRgBpCYcNuQbhAAQJQQ4jBwgEowmNCL8P3gQiAS8EUwwCCpIPkARHCV8I"
    "PwxWAQcPogjJDeIFDgVUB4QMzQ70BDUIrAFLCUIOBwPkBswDOQ/YCDIL6Qz7AeMN8ggoAGsGhgQ0"
    "B4gJtAU4ALgOwQ3iCyYIfwGvDEEEcwZHCDIMlAP3DyMLEQMOD4kKOQxYAksPYgFaDcgLUALSCk4O"
    "Lgg6AxgHrQCuDqIC0gV4Dk8D6AqYAHcCVA9rCygOXQAmCa0C9w2jBZEAnQSGCM4BigsSDjcG7Qet"
    "BLYGhQVLDOkJVQ4+AswKxQ9lDOkCHwrDBG8Dng/oBfAJowJ9DbEAsAVWAogHMgaWCCcEzAW3BAsI"
    "CAo7BpoA6wbHDIMFlwl0DREGigHtCwwNCgsCALIJ0wTWB3gG3gxACaABEQgBBIoGrQrCDDIHfw/u"
    "C7kOtwrwBEEBAwrRAIoOUgrrAmkB4gMYCP0LowA3BeoDmwh6BuQBTgeKAPwKgg4iBbgHcw97CSYN"
    "zwQCAZEMAwKGDmcA4AwkAw8L5w55BNoH3wI3AHEL9g7kCE8FtgepBqsNKwKjC2YNxgN2CrEE/AI6"
    "CmsNww/pAJwLiAPZCW8CXAbdB9gM0QKEDaYD5gv5D4wINA3TDuAFJwMbCZINjgFoC0MPQw0LCXkM"
    "0gOhCBsB2gt9CvQDnQujDiQKAQ57C5YJ3gbSD7gIUwUPAikJXgpZD+MDZQcyAk0KdQNMASwEGw/1"
    "CC4HxA+DAUwMfQ7/BtQLVgUSAskHfwS+CGUBnA0SBEIAuQ8mB7wFWgk8Aj0HWADhBPUKfAdwDzIK"
    "xwZgDs4H3AB0CgoFLg4nAgoHIg9NA/8B4AYOABAIzwJ1BpwDwgeSAbQL9gOIDVQOEwxwAScNjwYE"
    "DqsEpgylD6gIkgpdDOUA2gJlBScISgDxBcsICgG+DnIJAAZHDQQPvQaZCooFhAkODKAIPAtnBCcO"
    "CQaLC4oJ5QF4DBIBcATWDFsC6QVGBCkM4ALPBVQLngm4DEMGvA3qCHkFqQ9XATUJPgW0DVgKqAIl"
    "BhsAPgdQA94FiAjmABQLdglpAP8FhwsyA/8ERwYtCjYOMQu7CXkPTwSIAo4MZgMXCw8AywJsDBsI"
    "Tw4ZAloDswC2Do8BjAw2CH4D0A2mBsECbwW6CAILbQNwCaAOVwg+AO4PswdOBHkA7gTaDucKZwxg"
    "BA0NGAtFD48AkQTADqwMSgjiCWALZQTxD2AMQwUJCLUCQw5RDdYBSQi6DgINDAQAAj8DYgcuDZ4K"
    "PginBjUODQo4BX4L9QCgBE0POA0rCsYGOwWbCg8B7A5EBGEKNg8dDggApgeyD8oLUQH8CYIGaA2k"
    "AREOWQppCLEChQHKCUwHNQN3CC0CHwxvBwoJzwoABUIBjA6MAlgJ3wFxAyMP8gm+BmcF4A99CTEA"
    "twu0BjIJ2w0EDCMFjgAzDxQEuQGOB8gPqwM8CTgHDgsSBukD6AePDxsDAAn0DIgA4QdDCXwLHAYr"
    "DeEBDAdUBacMyQPcCv0ILwMGDPMGLA3LAy0GbgAFDrUF+wmoBjMNZAPIADwPlgbQDMgHkAoUB44N"
    "1gtFBKkA+AorB7sDjwLHB78EFgEXBqsOlwHuCcIFqAuaDbEISAJUBgAOmQxKAYoIfAL0C2UN8QFP"
    "By8G3QsTBVwBMQP6A0MKsATnDfgCRw9VB0YCMQUrD8QFDAHJD18Lng75B+ML+Q7/ACEExw/uBXAC"
    "zAvcA+kNkAVQAMIO9gU/AZgHuQx7DqQEewqDDfEOmgy6CpEIewJQC8oHAwNFCe4AkwQRDW0KVwAL"
    "Ax8F2glsDhgA/gp5CbQETg+KApcN/gZNDJcOSwiuABMJpwoVAZ8IoQvUDJkA7QnRB48EDQnMAVAK"
    "3QSrAkkL7giiAToOIArzB3cJuwEcAzsLmAToCEcKeQKFCPAAEQyHAbsF3QmFAJsPoAP7Bn4E+g+u"
    "DFEOIQcnDKUFcQ/iB94L7g7iDEkEiAZ/BSEOsAP9CeEKnAjbD74JLgKiBnYPjQv3BZ8NggTYDlAG"
    "/gNcDtYKVQJ+DIEF1g2bA+oG4QyJBwkF8gp1DAMHwASxD3QISQw/Db8DUw/sDVMDPQmAD5oGyQgA"
    "A1gHWAWTDVIMBwk9AGcGGgK9ClIDmQmLAQ8E1Ab4CMYBlgfsDxgBBAjEDKEAwQEpBLgFSQAIC/kE"
    "lgyVAqQD7wcfAJsJ2QFlCDMHow1wA68GsgC2CG0PNgCcCcAN1AJrCHUBKQCzDSgG1QnrAOIG2AGM"
    "C10GgAW3BygCPwsXDjIE6wvVAbcJOAH7DYMK1wNRBUEPfwBYCC8OTAtiAr4FsApXA6YLzAjQAgMM"
    "0AawDtAH5Az5AjQOngdrAdcJWQ7nBlYMjwpgDckCagspAX0PwgkdDaEHxgojAvwLlgW0Dl4EXA92"
    "A/gMCQteAtsOMwUTCKIJFQBtDBUKqAPsDMQAIAg7DwYLVwY3A/gEBwjRDtcIeQtjDYwG/gQRCh4P"
    "Ig16AK4N6ARLCgEP8gXSDQEFhgsvCW0G1gNYDccInAUWDwoCGAVjA74PiAUcCbkEBAYeDN0CSwRf"
    "Di4G4AMwAWkKngaTC70F1QgrBG4H4Q32Ar4K2A9uBKUNDQUgDxMGnwrRBHICLw21B5IO0wuDAtIM"
    "HwGMB0AEuAKCDMIAkAP4B3wJgw4ZBzYCXwFcCW4DlQphAu8AHA9RCs8LawBRBN0KswvnCDYBoQa9"
    "DJMA6g4WCJMKlQE9BYILVgj9DwgJQg2MAI8JVg6xAT4KvADpC/0F6AxeATAHxAKHAG0IUwEJB2UO"
    "5QgiAK4FWwm6ALoG5wnlBf0BTgqCDzAJUwebC44EkQFFBisMGQRGC3kHLQC6D2QIiQ3KBPQBIAfR"
    "DykI4QKRDVoHKg4tBM8J4Qs0AtUDiA5NCXkNyQDfBqAMQwPZB18CNQdeDM0CJQhQD8gEJwm1A5UI"
    "hA7GC1QJXA0gDO4CAQqNDKUDnA9HBPkKFQ+DA70NEgxvDjoFKgGmDdoFuw+0DPMCgQhuD1ANbAWG"
    "DFYEtQbvC8cJygVIA6MMKwE4BgUKfQA+D1AItQqFB/kNYQYnADoHFw+zAr8JwwEXBe0OyAp9BKcP"
    "TAXNDC8LzwZoAP0OIgJxBvsKlwWNBwAE5g9aBZoBoAZdChQCCA1MCM0EHwkGAOYGdghCA7QKOgLO"
    "CEEKSwAaBc4J5wAUCGMOhQkBA4wBxAfkDiQLDglrDuwE9wyjA9AFkwKCATYFCQPODH4IwAuvBWYK"
    "gwQ3C8wNDgb/C/EArQhmBmEDegEYDrMJLQxvClIE/w26AaUOPwKCCU0LyA2CCAEMBw5KB3MB2QLl"
    "DxwL/AP6DNcL/w7EBuQDPA4gC9AOhgadA3IKJgIWBi4P3AzMANkNaARqAs0GoQriAWYJNwxVC14N"
    "6Q+GCQULvQQFAo4D6wyED20HRwA3Ca4D9g33AeoJSA2nBwQEkAJ3BXIH2QBRA60LWgrJBk0AfASj"
    "B/wAEwMIBScGBwqxC4wF0wdpAjwGngGgCZ8AcAXMB8ABUQeFArUNNQxHAW8LuwgcBYIDPQpsCA0A"
    "uwumD94Hvw6jBHwGVQAaByIECAFWD7gN7AiABkQBXghwDJcCPwr5Bp8Lyg4FALgKjg9mCKkNngy9"
    "D0EIMg33BLkIUg8FDdEFmQ5SCWEPbACwDMcODQGPDTUKfw7gBCEI7w1ADDANRwOqCy0JuwSLDwEH"
    "EAQGDtcKOwdJBm4MagVqDd4DOwGzCP8CMQp2Dv4IcwwbBhwKiweVAH4OCgMxBD4OfAXTDxUIPwS/"
    "BQYJ5wQ9BnoLqQGsBN4JIAa2AiEBLAx/A6YKhwKlCx0EBwv+B54D2ghsBB0HRglcAJUPTwupAnQE"
    "5Qn4D/QF8QwXAOYHmwXxCZgCZQDoD94Bsg65CccCcAf5BdcMwwDFDb4HhwPEAcgOvQJKBTQMjAmQ"
    "C5oKxQZ5ASENqAAFA5sMUgJADugAbQkaAwsHLAAADzQL2wckDkgGqwBgCN0GtwGFDWwCqgYlDtQB"
    "8Qr9AooMwwMMBgkJ+AbaAIgBhwj3A/cKzA4EAfcLKghKDRQJRwskBPkAFg5kCoULbw9BBeMKQgKi"
    "BX0LRQhODdEKcgQVAtsFOg/UCBcM1QSvCfAKZA8/B/sLnwMYDZYOxwrCCOUDjwVqCVkE2Q71CYMM"
    "wQ90Ba4JXAyUCsgFsw/qCzkFaQ2PB/oBCg/pCtMNEgVjCiwONQKHCaQMWwN8DpQExgXxAuQHiwbd"
    "CMkE8wHxBk4JOgRFDJMP3AkRAJMGtAPQD/YHoQ01AIwDZgKZBxsOXwaaCFIBTAr8B6QGMgUoDAcC"
    "7Q3JDIYB/AYYAiID1gTiDeAAcweLBC4AOAMPCPgAbwavCHkKtwAaDGcD6geQDLgGBgOCBzUGJgW9"
    "Ab8GVQodAfUO1QseDVEPQQCVDDQDkQ5JAX0IEgfFBBkOfQFMCZwMLQEOBx0F7wkzCw8PzwPdAUAF"
    "uw1MBPwOggKWAIgPZAcoCnQA/g/bC20NGQnxB2cKvgNtDvwIKA/tDNMJ1Q2AAmsPaQQwDm0FtAkc"
    "AqcOiwCaCxEPOw3TCK0PpAt0B8MMWQkgBUMCvwq9A/sHwQlvDUsGtgDnDEADvAt3B6kOngK9CIYK"
    "jQ4JDQ0GhABKCUUNrwvFAPsFvAlLC24IYQQqBkwDEQWuB6kKtgUMAFcLdgEBBuQLLwItCxMH7wQG"
    "BDoJiQuNAVoImwY3DRUE2QVeCbgEEAHHA9UKQAA1BCUCAw6RA34ALQcQBlIO/gtuBZsCNgvcDvcI"
    "UwIqCn4FEwsxBqIE8AshA8IBXQhYDBcH1gJwCp0PSwPfDH4BUw46DSwJdwuQDX0CqgiAAyYPhQb+"
    "DJQP9QJ7CEEGnwGLDsMKVgDFDDEHHgNIDyoAGwvOD0AIUQw8CpoCwQfKDdQJIwY8CA0Ltw8ECrQI"
    "cQRoAfwPXAdiCtgD6wWZDa8PHgR1AC0NaA/bAL0HOAT0D18FXg52BAoInQYdCYEHdQWcCtUCHgEx"
    "DyUErAacDq8EQQxcApsHYwksBXcNxgBjDHsDuwf1BQwOBAVXCt8L0AilAlAHWQEeDuEGXg+yBW8M"
    "EgMhD7IEmAEQDOICeg3KAHILHgnaAbUEagxDAOkHqgEdDDoIWQPLCdIGuQ1sC6oJBgHJChcCogxy"
    "ACMOOAICBGsM7wYcCOgJHAygAG8JkAHHDQwK8wMBAbMKSgTpDg8KyAjvD9wC5gGDCcQONQGSBOoM"
    "+AlrA0YF2wGkCHUEZAukAAUJGg3MBp0FhQ6vB58MlQZ4A7oNOwhPD7YJ1wYUBWUJag6lCtcBNQXr"
    "CHYCewaFAwIMpwhAD/wEcAtCCt0PMADDDsAF4AFkDhINzwcDC2YF6Ab+DpELGg4fCKMGZwJCBTkL"
    "YQ0hDEwGqgPYB54F7g1bBo4LRA2xCp0OnQlqAUEHYQ6dCuMAiwnnAwQCBQXXDuQKkQUUAaILKwPv"
    "DAMPiwJCB/AF9A07DC0PEwBUDfcOSAfVBWcBrwN4DSYGnQjGBLYLaAO7CvsEKQbYArgPVADpCC0D"
    "/gHNBSYA6AsVDWAHhgA+BE8I8wALC5QMFgITD90AIAkgBBAAgQKDBnwNwANQBQYIpgJmD0ELfwg7"
    "CgAAeAk3B1gOHwIhBu4K/QDTA2ULogBQBOQC4gokCJkEMAq8AlEJAw3kCdUH7AJPAagJsg1WB/AI"
    "9wDQA08KvwtkBEkNSgzfB7UJsA+4Az8JbwE4D0AK7QY/DqIPOQkoB44K9AIFCIEPxwW/DEMIqA+r"
    "C/kBZgxqBPUM7wUVDjsDig9zAgENDgTDCBAKnASqDaYIgQzXDzsJfwc2BlQBiQxOBZoOWgAiC7cG"
    "5wEaD0gMjga5AI0Chw84CJIMHAcxAlwIhw5PBtAEwg3AAoQKqgR1DrMF7QhBAukENgM8ADMExwsG"
    "BTIOJQxeBxYLzgT6AvQJMwaODtsIeADyBkABigc+DKcEaQbnC8wPUgCDB7cOfQZLASUFCgoXDZUD"
    "Sg5cCwICuQfmA5oPiATqDVcFwgqADkgEKA1aC3EB/Q30DpkFyAmtAb4AHQs5BzEByAx+BlEL9wKk"
    "DbIL9gzUBeMJvw2OCHsBsAamAIsDMwo+ASsOTAB9B84KWAPCC/8Jng1rBbkKAQnAAPQHXwM8DWQF"
    "tAKCCv8Hcg5ZAnYAYw9vCH4JmQZ1DTwM0QjkAMULUgiKAzgJvAeqBfYJfwZTCW8AagPlDKQK/wNE"
    "D/QIegXzDnIIrwGcB+0DwQCiCuAHgQZpD5kCVQ2ACeEORwK3CDYNmAVVCfoOpgEhBe0PSQI0CMQD"
    "yQELD/MNfgp4AT0LNAk9DL4BrAPLC7MGwQq3BVwE8gLHAGsK3wUpA0sHIwp1AgEAvgzNAeIO+wIJ"
    "BBUFFAyyB/AP2AahAnYNrAt0A2AAFgoUDo8MjQnhD2MBaQ7sAToMWQXQCg0E5gV0C9kPWgTKBp0M"
    "8ANoCFkNkgaNBLUOMAsTDeMC3QXZBCkHLA80BBkGpA+GDZYE+QjGDMUB1A0KDOoP1wRFDj0B7w4g"
    "DUYG1Q8HB8QE+gvsCpYPVg16AjoLnwSSCMkFkQn1AQIIRAxYBG4CCAYkBdEGrAiEBD4LtwNbADMI"
    "ygyQB+QNxwH9B/IL6gC7AhoLnwmvAEMMFwkvAMAG4AmXCK0MTwIiDt8AZwi4CToAXQfMAhQP5wcd"
    "CgUH4ghoAg0IDAtfCfIE+QNFC6cJ5Q2nAA4ISgqxBvsIhAFXDpQAYgyPDtoEcgbAD0MHEgvODgMA"
    "CQwNA9oMUAk8B8EOGArXAgkB8wSpCT0DdQq3DYUPRgfqBUQOFQOoB6cFkQ8aAfID0QtJCl4GHQP7"
    "DCoLggUQDoELJwEtBdEDHgBtC+YMmQPLBhEC+AuiDWYB0wK8CBUGDgI="
)
_BLUE_NOISE = (
    np.frombuffer(base64.b64decode(_BLUE_NOISE_B64), dtype="<u2")
    .reshape(_BLUE_NOISE_SIZE, _BLUE_NOISE_SIZE)
    .astype(np.float32)
)
# Normalized to a zero-mean -0.5..~0.5 range by the tile's OWN value count
# (4096 ranks), same treatment as `_BAYER_NORM` — but `blue_noise_offset`
# then rescales by the same `strength * 64.0` constant `bayer_offset` uses
# (not by 4096), so a given `dither_strength` produces the same offset
# magnitude regardless of which ordered method is selected.
_BLUE_NOISE_NORM = (_BLUE_NOISE / float(_BLUE_NOISE_SIZE * _BLUE_NOISE_SIZE)) - 0.5

# Floyd-Steinberg error-diffusion kernel: (dx, dy, weight/16), applied to the
# 4 unvisited neighbors of a raster-scan pixel.
_FS_KERNEL: tuple[tuple[int, int, float], ...] = (
    (1, 0, 7 / 16),
    (-1, 1, 3 / 16),
    (0, 1, 5 / 16),
    (1, 1, 1 / 16),
)
# Atkinson diffuses only 3/4 of the error (the rest is dropped), which keeps
# contrast punchier than Floyd-Steinberg at the cost of losing some detail in
# deep shadows/highlights — the classic Mac-era look.
_ATKINSON_KERNEL: tuple[tuple[int, int, float], ...] = tuple(
    (dx, dy, 1 / 8) for dx, dy in ((1, 0), (2, 0), (-1, 1), (0, 1), (1, 1), (0, 2))
)

_ERROR_DIFFUSION_KERNELS = {
    "floyd-steinberg": _FS_KERNEL,
    "atkinson": _ATKINSON_KERNEL,
}


def bayer_offset(h: int, w: int, strength: float) -> np.ndarray:
    """Return an (h, w) float32 additive offset from the tiled 8×8 Bayer matrix.

    Add this to a pixel array's every channel before nearest-palette
    quantization: pixels below the local threshold get pushed toward the next
    palette entry down, pixels above toward the next one up, and the fixed
    8×8 tiling means the same screen position always gets the same push — so
    a static source dithers identically frame to frame (no shimmer) while a
    moving one still gets full ordered-dither texture. `strength` scales the
    offset's total range (roughly ±32 * strength at strength's face value)."""
    tiles_y = -(-h // 8)
    tiles_x = -(-w // 8)
    tiled = np.tile(_BAYER_NORM, (tiles_y, tiles_x))[:h, :w]
    return tiled * (strength * 64.0)


def blue_noise_offset(h: int, w: int, strength: float) -> np.ndarray:
    """Return an (h, w) float32 additive offset from the tiled 64×64 blue-noise
    matrix — same call-site contract as `bayer_offset` (add to a pixel
    array's every channel before nearest-palette quantization, same
    `strength` scale), but tiling a void-and-cluster mask instead of the
    regular Bayer grid: no periodic low-frequency structure, so it dithers
    without Bayer's visible cross-hatch at C64 resolution while keeping the
    same vectorized, position-deterministic (no frame-to-frame shimmer)
    properties. See the module docstring and `scripts/diags/gen_blue_noise.py`."""
    tiles_y = -(-h // _BLUE_NOISE_SIZE)
    tiles_x = -(-w // _BLUE_NOISE_SIZE)
    tiled = np.tile(_BLUE_NOISE_NORM, (tiles_y, tiles_x))[:h, :w]
    return tiled * (strength * 64.0)


def error_diffuse(
    img_bgr: np.ndarray,
    candidates_bgr: np.ndarray,
    method: str,
    strength: float = 1.0,
) -> np.ndarray:
    """Floyd-Steinberg / Atkinson dither `img_bgr` against a fixed candidate set.

    img_bgr: (h, w, 3) BGR, any numeric dtype. candidates_bgr: (k, 3) BGR.
    Returns an (h, w) uint8 array of indices into `candidates_bgr` (0..k-1).

    A plain per-pixel raster scan (Python-level loop — not realtime; see the
    module docstring): at each pixel, picks the nearest candidate by squared
    BGR distance, then pushes the quantization error onto not-yet-visited
    neighbors per `method`'s kernel, scaled by `strength` (1.0 = the
    textbook kernel weights; lower softens the diffusion toward a flatter,
    more `ordered`-like result).
    """
    kernel = _ERROR_DIFFUSION_KERNELS.get(method)
    if kernel is None:
        raise ValueError(
            f"error_diffuse: method must be one of {tuple(_ERROR_DIFFUSION_KERNELS)}, got {method!r}"
        )
    h, w = img_bgr.shape[:2]
    buf = img_bgr.astype(np.float32).copy()
    cand = np.asarray(candidates_bgr, dtype=np.float32)
    codes = np.empty((h, w), dtype=np.uint8)
    for y in range(h):
        for x in range(w):
            px = buf[y, x]
            d = ((cand - px) ** 2).sum(axis=1)
            idx = int(np.argmin(d))
            codes[y, x] = idx
            err = (px - cand[idx]) * strength
            for dx, dy, frac in kernel:
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and 0 <= ny < h:
                    buf[ny, nx] += err * frac
    return codes


def error_diffuse_cells(
    pixels: np.ndarray,
    candidates: np.ndarray,
    method: str,
    strength: float = 1.0,
) -> np.ndarray:
    """Batched per-cell error diffusion: N independent small regions, each
    diffused against its OWN candidate set, in lockstep across cells.

    pixels: (N, H, W, 3) BGR — N independent cells (no diffusion carries
    across a cell boundary, matching each display cell picking its own
    {bg0, c1, c2, ...} palette subset). candidates: (N, K, 3) BGR, one
    K-color candidate set per cell (K constant across cells; pad short
    per-cell sets by repeating an existing candidate — a repeat can only tie,
    never win a pixel it wouldn't otherwise). Returns (N, H, W) uint8 codes,
    each 0..K-1 indexing that cell's own candidate row.

    Loops over the H*W in-cell positions (small: 32 pixels for an mhires
    cell, 4 for MCM) with every step vectorized across all N cells at once,
    rather than looping N times with a small per-cell scan — same math, far
    fewer Python-level iterations.
    """
    kernel = _ERROR_DIFFUSION_KERNELS.get(method)
    if kernel is None:
        raise ValueError(
            f"error_diffuse_cells: method must be one of {tuple(_ERROR_DIFFUSION_KERNELS)}, got {method!r}"
        )
    n, h, w = pixels.shape[:3]
    buf = pixels.astype(np.float32).copy()
    cand = np.asarray(candidates, dtype=np.float32)
    codes = np.empty((n, h, w), dtype=np.uint8)
    for y in range(h):
        for x in range(w):
            px = buf[:, y, x, :]  # (N, 3)
            d = ((cand - px[:, None, :]) ** 2).sum(axis=2)  # (N, K)
            idx = d.argmin(axis=1)  # (N,)
            codes[:, y, x] = idx
            chosen = np.take_along_axis(cand, idx[:, None, None], axis=1)[:, 0, :]  # (N, 3)
            err = (px - chosen) * strength
            for dx, dy, frac in kernel:
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and 0 <= ny < h:
                    buf[:, ny, nx, :] += err * frac
    return codes
