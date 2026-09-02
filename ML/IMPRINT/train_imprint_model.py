"""build_dataset.py가 만든 ML/IMPRINT/data/labels.csv로 각인(imprint) 인식
CRNN(CNN+BiLSTM) + CTC 모델을 학습한다.

## 왜 CRNN+CTC인가
지금 EasyOCR 방식은 82.5%가 "빈 후보"(진단 스크립트 확인) - 범용 OCR 텍스트
검출기가 저대비 양각/음각 각인 자체를 텍스트 영역으로 못 찾는 게 근본 원인.
글자 단위 위치 라벨링 없이도 (이미지, 정답 문자열) 쌍만으로 학습되는
CTC(Connectionist Temporal Classification) 방식을 쓰면, build_dataset.py가
이미 만들어둔 데이터 그대로 바로 학습할 수 있다 - 글자 하나하나의 바운딩
박스를 사람이 새로 그릴 필요가 없다.

## 문자 집합(vocabulary)과 "분할선" 토큰
각인은 A-Z, 0-9만 실제로 알약에 찍혀있다. "분할선"은 물리적으로 존재하는
문자가 아니라 "여기 홈(스코어라인)이 있다"는 표시로 DB/기존 코드가 끼워넣는
텍스트 토큰이다(app/pill_analyze.py의 extract_imprint 참고) - 그래서 3글자
그대로("분","할","선") 개별 학습시키면 안 되고, 하나의 특수 클래스(DIV_TOKEN)로
취급해야 한다. VOCAB = A-Z(26) + 0-9(10) + DIV_TOKEN(1) = 37클래스, CTC blank
포함 38.

## 사용법
로컬(CPU)에서도 돌아가지만 느리다 - 실제 학습은 Colab(GPU) 권장.
  venv/Scripts/python.exe ML/IMPRINT/train_imprint_model.py --epochs 30
  (Colab) !python ML/IMPRINT/train_imprint_model.py --epochs 30 --batch-size 64
"""

import argparse
import csv
import random
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn as nn
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data import DataLoader, Dataset

DATA_DIR = Path(__file__).resolve().parent / "data"
LABELS_CSV = DATA_DIR / "labels.csv"
MODELS_DIR = Path(__file__).resolve().parent / "models"
DEFAULT_OUTPUT = MODELS_DIR / "imprint_crnn.pt"

# 입력 이미지 크기(고정) - 높이를 고정해야 CNN이 시간축(너비)만 남기고 특징을
# 뽑을 수 있다. 각인 크롭이 대체로 정사각形에 가까워서 폭을 넉넉히 준다.
IMG_HEIGHT = 32
IMG_WIDTH = 128

ALPHABET = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
DIV_TOKEN = "분할선"
VOCAB = ALPHABET + [DIV_TOKEN]  # 인덱스 0..len(VOCAB)-1 = 실제 클래스
BLANK_IDX = len(VOCAB)  # CTC blank는 맨 뒤 인덱스
NUM_CLASSES = len(VOCAB) + 1  # +1 = blank


def tokenize(label: str) -> list:
    """"V분할선8" -> [VOCAB의 'V' 인덱스, DIV_TOKEN 인덱스, '8' 인덱스].
    VOCAB에 없는 문자(공백, 하이픈 등 - 실측 확인된 print_front 예외값들)는
    건너뛴다 - 완벽하지 않은 라벨 몇 글자보다 학습 안정성이 우선."""
    tokens = []
    i = 0
    n = len(label)
    while i < n:
        if label[i : i + 3] == DIV_TOKEN:
            tokens.append(VOCAB.index(DIV_TOKEN))
            i += 3
            continue
        ch = label[i].upper()
        if ch in ALPHABET:
            tokens.append(ALPHABET.index(ch))
        i += 1
    return tokens


def detokenize(indices: list) -> str:
    """CTC 디코딩 결과(중복/blank 제거된 인덱스 리스트)를 사람이 읽을 문자열로."""
    return "".join(VOCAB[i] for i in indices)


def _load_image_tensor(image_path: str) -> torch.Tensor:
    image = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
    if image is None:
        return None
    image = cv2.resize(image, (IMG_WIDTH, IMG_HEIGHT), interpolation=cv2.INTER_AREA)
    image = image.astype(np.float32) / 255.0
    image = (image - 0.5) / 0.5  # [-1, 1]로 정규화
    return torch.from_numpy(image).unsqueeze(0)  # (1, H, W)


class ImprintDataset(Dataset):
    def __init__(self, rows: list):
        # rows = [(image_path, label_text), ...] - 토큰화도 미리 해둔다(라벨 오류
        # 필터링을 __init__ 시점에 한 번만 하기 위함).
        self.samples = []
        for image_path, text in rows:
            tokens = tokenize(text)
            if tokens:
                self.samples.append((image_path, tokens))

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        image_path, tokens = self.samples[idx]
        image = _load_image_tensor(image_path)
        if image is None:
            # 드물게 파일이 깨졌으면 검은 이미지로 대체(배치 크기를 흔들지 않기 위함) -
            # CTC는 어차피 이 샘플에서 큰 손실이 나서 자연스럽게 무시된다.
            image = torch.zeros(1, IMG_HEIGHT, IMG_WIDTH)
        return image, torch.tensor(tokens, dtype=torch.long)


def _collate(batch):
    images, label_seqs = zip(*batch)
    images = torch.stack(images)  # (B, 1, H, W)
    label_lengths = torch.tensor([len(s) for s in label_seqs], dtype=torch.long)
    labels_concat = torch.cat(label_seqs)  # CTCLoss는 라벨을 이어붙인 1D 텐서로 받는다
    return images, labels_concat, label_lengths


class CRNN(nn.Module):
    """작은 CNN으로 특징을 뽑고, 높이를 1로 눌러 시간축(너비)만 남긴 뒤
    BiLSTM으로 시퀀스를 읽는다 - 표준 scene-text recognition 구조(CRNN)의
    축소판. 알약 각인은 보통 1~5글자로 짧아서 레이어를 깊게 쌓지 않았다."""

    def __init__(self, num_classes: int):
        super().__init__()
        self.cnn = nn.Sequential(
            nn.Conv2d(1, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(), nn.MaxPool2d(2, 2),  # H/2, W/2
            nn.Conv2d(32, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(), nn.MaxPool2d(2, 2),  # H/4, W/4
            nn.Conv2d(64, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(),
            nn.Conv2d(128, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(), nn.MaxPool2d((2, 1)),  # H/8, W/4
            nn.Conv2d(128, 256, 3, padding=1), nn.BatchNorm2d(256), nn.ReLU(),
            nn.AdaptiveAvgPool2d((1, None)),  # 높이를 1로 완전히 눌러 시간축만 남김
        )
        self.rnn = nn.LSTM(256, 128, num_layers=2, bidirectional=True, batch_first=True)
        self.fc = nn.Linear(128 * 2, num_classes)

    def forward(self, x):
        feat = self.cnn(x)  # (B, C, 1, W')
        feat = feat.squeeze(2).permute(0, 2, 1)  # (B, W', C) - W'가 시간축
        out, _ = self.rnn(feat)
        return self.fc(out)  # (B, W', num_classes)


def _ctc_greedy_decode(ids) -> list:
    """ids: 이미 argmax된 클래스 인덱스 시퀀스(1D, 길이 T) 한 샘플.
    blank/연속 중복 제거 후 인덱스 리스트를 돌려준다."""
    if torch.is_tensor(ids):
        ids = ids.tolist()
    decoded = []
    prev = None
    for i in ids:
        if i != prev and i != BLANK_IDX:
            decoded.append(i)
        prev = i
    return decoded


def _load_rows() -> list:
    if not LABELS_CSV.is_file():
        raise FileNotFoundError(f"{LABELS_CSV} 가 없습니다 - 먼저 build_dataset.py를 실행하세요.")
    rows = []
    with open(LABELS_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append((row["image_path"], row["text"], row["item_seq"]))
    return rows


def _split_train_val(rows: list, val_ratio: float = 0.15, seed: int = 42):
    """item_seq 단위로 나눈다(같은 알약의 front/back이 train/val에 걸쳐 섞이지
    않게) - 모양 학습 때 StratifiedGroupKFold를 쓴 것과 같은 이유."""
    by_item = defaultdict(list)
    for image_path, text, item_seq in rows:
        by_item[item_seq].append((image_path, text))

    item_seqs = list(by_item.keys())
    random.Random(seed).shuffle(item_seqs)
    val_count = max(1, int(len(item_seqs) * val_ratio))
    val_items = set(item_seqs[:val_count])

    train_rows, val_rows = [], []
    for item_seq, samples in by_item.items():
        target = val_rows if item_seq in val_items else train_rows
        target.extend(samples)
    return train_rows, val_rows


def train(epochs: int, batch_size: int, lr: float, output_path: Path) -> None:
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"디바이스: {device}")

    rows = _load_rows()
    print(f"labels.csv 총 {len(rows)}건")
    train_rows, val_rows = _split_train_val(rows)
    print(f"train {len(train_rows)}건 / val {len(val_rows)}건 (item_seq 단위 분할)")

    train_ds = ImprintDataset(train_rows)
    val_ds = ImprintDataset(val_rows)
    print(f"토큰화 후 유효 샘플: train {len(train_ds)}건 / val {len(val_ds)}건")

    train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True, collate_fn=_collate, num_workers=0)
    val_loader = DataLoader(val_ds, batch_size=batch_size, shuffle=False, collate_fn=_collate, num_workers=0)

    model = CRNN(NUM_CLASSES).to(device)
    criterion = nn.CTCLoss(blank=BLANK_IDX, zero_infinity=True)
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)

    best_val_acc = -1.0
    for epoch in range(1, epochs + 1):
        model.train()
        total_loss = 0.0
        for images, labels_concat, label_lengths in train_loader:
            images, labels_concat, label_lengths = images.to(device), labels_concat.to(device), label_lengths.to(
                device
            )
            optimizer.zero_grad()
            logits = model(images)  # (B, T, C)
            log_probs = logits.log_softmax(dim=-1).permute(1, 0, 2)  # CTCLoss는 (T, B, C)
            input_lengths = torch.full((images.size(0),), log_probs.size(0), dtype=torch.long, device=device)
            loss = criterion(log_probs, labels_concat, input_lengths, label_lengths)
            loss.backward()
            optimizer.step()
            total_loss += loss.item() * images.size(0)

        avg_loss = total_loss / max(len(train_ds), 1)

        model.eval()
        correct = 0
        with torch.no_grad():
            for images, labels_concat, label_lengths in val_loader:
                images = images.to(device)
                logits = model(images)
                preds = logits.argmax(dim=-1).cpu()
                offset = 0
                for i, length in enumerate(label_lengths.tolist()):
                    target = labels_concat[offset : offset + length].tolist()
                    offset += length
                    pred_tokens = _ctc_greedy_decode(preds[i])
                    if pred_tokens == target:
                        correct += 1
        val_acc = correct / max(len(val_ds), 1)

        print(f"[epoch {epoch}/{epochs}] loss={avg_loss:.4f} val_exact_acc={val_acc*100:.1f}%")

        if val_acc > best_val_acc:
            best_val_acc = val_acc
            output_path.parent.mkdir(parents=True, exist_ok=True)
            torch.save({"model_state": model.state_dict(), "vocab": VOCAB}, output_path)
            print(f"  best 갱신 - 저장: {output_path}")

    print(f"\n학습 완료. best val_exact_acc={best_val_acc*100:.1f}%, 모델: {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    train(args.epochs, args.batch_size, args.lr, args.output)
