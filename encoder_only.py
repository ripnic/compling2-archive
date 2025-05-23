!pip install datasets

from collections import defaultdict
from urllib import request
import json
import pandas as pd
from math import ceil
from tqdm.auto import tqdm
import random
import torch
import numpy as np
import re
SPACE_PATTERN = re.compile(r'[\n\s]+') #removing space and new lines
from transformers import AutoModel, AutoTokenizer
import datasets

# Fixing random seeds
def set_seed(seed=42):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)  # if using GPU
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False

def parse_conllu_using_pandas(block):
    records = []
    for line in block.splitlines():
        if not line.startswith('#'):
            records.append(line.strip().split('\t'))
    return pd.DataFrame.from_records(
        records,
        columns=['ID', 'FORM', 'TAG', 'Misc1', 'Misc2'])

def tokens_to_labels(df):
    return (
        df.FORM.tolist(),
        df.TAG.tolist()
    )

PREFIX = "https://raw.githubusercontent.com/UniversalNER/"
DATA_URLS = {
    "en_ewt": {
        "train": "UNER_English-EWT/master/en_ewt-ud-train.iob2",
        "dev": "UNER_English-EWT/master/en_ewt-ud-dev.iob2",
        "test": "UNER_English-EWT/master/en_ewt-ud-test.iob2"
    },
    "en_pud": {
        "test": "UNER_English-PUD/master/en_pud-ud-test.iob2"
    }
}

# en_ewt is the main train-dev-test split
# en_pud is the OOD test set
data_dict = defaultdict(dict)
for corpus, split_dict in DATA_URLS.items():
    for split, url_suffix in split_dict.items():
        url = PREFIX + url_suffix
        with request.urlopen(url) as response:
            txt = response.read().decode('utf-8')
            data_frames = map(parse_conllu_using_pandas,
                              txt.strip().split('\n\n'))
            token_label_alignments = list(map(tokens_to_labels,
                                              data_frames))
            data_dict[corpus][split] = token_label_alignments

# Saving the data so that you don't have to redownload it each time.
with open('ner_data_dict.json', 'w', encoding='utf-8') as out:
    json.dump(data_dict, out, indent=2, ensure_ascii=False)

# Each subset of each corpus is a list of tuples where each tuple
# is a list of tokens with a corresponding list of labels.

# Train on data_dict['en_ewt']['train']; validate on data_dict['en_ewt']['dev']
# and test on data_dict['en_ewt']['test'] and data_dict['en_pud']['test']
data_dict['en_ewt']['train'][0]

# Converting data for input to tuples of token and label
def convert_to_token_label_pairs(dataset):
    converted = []
    for tokens, labels in dataset:
        sentence = [[token, label] for token, label in zip(tokens, labels)]
        converted.append(sentence)
    return converted

# Converting all datasets
training_data = convert_to_token_label_pairs(data_dict['en_ewt']['train'])
validating_data = convert_to_token_label_pairs(data_dict['en_ewt']['dev'])
testing_data = convert_to_token_label_pairs(data_dict['en_ewt']['test'])
OOD_testing_data = convert_to_token_label_pairs(data_dict['en_pud']['test'])

# Check first sentence
print(training_data[0])

# Finding out how many different labels we have
labels = set()
for ex in data_dict['en_ewt']['train']:
  _, label_list = ex
  labels.update(label_list)
n_classes = len(labels)
sorted(labels)

# The models expect class numbers, not strings
label_to_i = {
    label: i
    for i, label in enumerate(sorted(labels))
}
i_to_label = {
    i: label
    for label, i in label_to_i.items()
} #Convert and then convert back

n_classes = len(label_to_i)
print(f'There are {n_classes} classes.')

# Downloading BERT-type model
model_tag = 'google-bert/bert-base-uncased'
encoder = AutoModel.from_pretrained(model_tag)
tokeniser = AutoTokenizer.from_pretrained(model_tag)

# Our data is pretokenised, which we can use
example_input = [el[0] for el in training_data[0]]
example_output = [el[1] for el in training_data[0]]
example_tokenisation = tokeniser(example_input, is_split_into_words=True)

# Checking for subword embeddings

print(tokeniser.decode(example_tokenisation.input_ids))
for input_id in example_tokenisation.input_ids:
    print(tokeniser.decode([input_id]), end=' ')

# Shuffling data and getting batches with DataLoader
from torch.utils.data import DataLoader

def collate_fn(batch):
    return batch

generator = torch.Generator()
generator.manual_seed(42)

shuffled_training_data = DataLoader(training_data, batch_size=32,
                                    shuffle=True, generator=generator,
                                    collate_fn=collate_fn)

# Setting random seed
set_seed(42)

encoder.cuda();

import torch
import torch.nn as nn

class ClassificationHead(nn.Module):
    def __init__(self, model_dim=768, n_classes=n_classes):
        super().__init__()
        self.linear = nn.Linear(model_dim, n_classes)
        self.dropout = nn.Dropout(0.2)

    def forward(self, x):
        return self.linear(x)

clf_head = ClassificationHead()
clf_head.cuda();
optim = torch.optim.AdamW(
    list(encoder.parameters()) + list(clf_head.parameters()),
    lr=10**(-5))
loss = nn.CrossEntropyLoss()

def process_batch(sentences, label_to_i, tokeniser, encoder, clf_head,
                  encoder_device, clf_head_device):
    all_logits = []
    all_gold_labels = []

    encoder.eval()
    clf_head.eval()

    for sentence in sentences:
        gold_labels = torch.tensor(
            [label_to_i[label] for _, label in sentence]).to(clf_head_device)
        words = [word for word, _ in sentence]

        # Tokenize the sentence
        tokenisation = tokeniser(words, is_split_into_words=True,
                                 return_tensors='pt', truncation=True)
        inputs = {k: v.to(encoder_device) for k, v in tokenisation.items()}

        # Get encoder output
        with torch.no_grad():
            outputs = encoder(**inputs).last_hidden_state[0, 1:-1, :]  # Remove CLS/SEP

        word_ids = tokenisation.word_ids()[1:-1]  # Also ignore CLS/SEP
        processed_words = set()
        first_subword_embeddings = []

        for i, word_id in enumerate(word_ids):
            if word_id is not None and word_id not in processed_words:
                first_subword_embeddings.append(outputs[i])
                processed_words.add(word_id)

        # Check alignment
        assert len(first_subword_embeddings) == gold_labels.size(0)

        # Run through classifier
        clf_inputs = torch.vstack(first_subword_embeddings).to(clf_head_device)
        logits = clf_head(clf_inputs)

        all_logits.append(logits)
        all_gold_labels.append(gold_labels)

    # Concatenate all logits and labels in the batch
    all_logits = torch.cat(all_logits, dim=0)
    all_gold_labels = torch.cat(all_gold_labels, dim=0)

    return all_logits, all_gold_labels

def train_epoch(data, label_to_i, tokeniser, encoder, clf_head,
               encoder_device, clf_head_device, loss_fn, optimiser):
  encoder.train()
  epoch_losses = torch.empty(len(data))
  for step_n, sentence in tqdm(
      enumerate(data),
      total=len(data),
      desc='Train',
      leave=False
  ):
    optimiser.zero_grad()
    logits, gold_labels = process_batch(
        sentence, label_to_i, tokeniser, encoder, clf_head,
        encoder_device, clf_head_device)
    loss = loss_fn(logits, gold_labels)
    loss.backward()
    optimiser.step()
    epoch_losses[step_n] = loss.item()
  return epoch_losses.mean().item()

def validate_epoch(data, label_to_i, tokeniser, encoder, clf_head,
               encoder_device, clf_head_device):
  encoder.eval()
  epoch_accuracies = torch.empty(len(data))
  for step_n, sentence in tqdm(
      enumerate(data),
      total=len(data),
      desc='Eval',
      leave=False
  ):
      with torch.no_grad():
        logits, gold_labels = process_batch(
            [sentence], label_to_i, tokeniser, encoder, clf_head,
            encoder_device, clf_head_device)
        predicted_labels = logits.argmax(dim=-1)
        epoch_accuracies[step_n] = (
            predicted_labels == gold_labels).sum().item() / len(sentence)
  return epoch_accuracies.mean().item()

encoder_device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
encoder = AutoModel.from_pretrained(model_tag).to(encoder_device)
clf_head_device = encoder_device
clf_head = ClassificationHead(n_classes=n_classes).to(clf_head_device)

n_epochs = 5
loss_fn = nn.CrossEntropyLoss()
optimiser = torch.optim.AdamW(
    list(encoder.parameters()) + list(clf_head.parameters()),lr=5e-6, weight_decay=0.01)
for epoch_n in tqdm(range(n_epochs)):
    loss = train_epoch(shuffled_training_data, label_to_i, tokeniser, encoder, clf_head,
                       encoder_device, clf_head_device, loss_fn, optimiser)
    print(f'Epoch {epoch_n+1} training loss: {loss:.2f}')
    accuracy = validate_epoch(validating_data, label_to_i, tokeniser, encoder, clf_head,
                       encoder_device, clf_head_device)
    print(f'Epoch {epoch_n+1} dev accuracy: {accuracy:.2f}')

from collections import defaultdict, Counter

# First, extract BIO spans
def extract_spans(label_seq):
    spans = []
    start = None
    current_label = None
    for i, tag in enumerate(label_seq):
        if tag.startswith('B-'):
            if start is not None:
                spans.append((start, i - 1, current_label))
            start = i
            current_label = tag[2:]
        elif tag.startswith('I-'):
            if current_label is None:
                start = i
                current_label = tag[2:]
        else:
            if start is not None:
                spans.append((start, i - 1, current_label))
                start = None
                current_label = None
    if start is not None:
        spans.append((start, len(label_seq) - 1, current_label))
    return spans

def process_sentence(sentence, label_to_i, tokeniser, encoder, clf_head,
                      encoder_device, clf_head_device):
    gold_labels = torch.tensor(
        [label_to_i[label] for _, label in sentence]).to(clf_head_device)
    words = [word for word, _ in sentence]

    # Tokenize the sentence
    tokenisation = tokeniser(words, is_split_into_words=True,
                             return_tensors='pt', truncation=True)
    inputs = {k: v.to(encoder_device) for k, v in tokenisation.items()}

    # Get encoder output
    with torch.no_grad():
        outputs = encoder(**inputs).last_hidden_state[0, 1:-1, :]  # Remove CLS/SEP

    word_ids = tokenisation.word_ids()[1:-1]  # Also ignore CLS/SEP
    processed_words = set()
    first_subword_embeddings = []

    for i, word_id in enumerate(word_ids):
        if word_id is not None and word_id not in processed_words:
            first_subword_embeddings.append(outputs[i])
            processed_words.add(word_id)

    # Check alignment
    assert len(first_subword_embeddings) == gold_labels.size(0)

    # Run through classifier
    clf_inputs = torch.vstack(first_subword_embeddings).to(clf_head_device)
    logits = clf_head(clf_inputs)

    return logits, gold_labels

def get_predictions(data, label_to_i, i_to_label, tokeniser, encoder, clf_head,
                    encoder_device, clf_head_device):
    all_preds = []
    all_golds = []

    # Set the models to evaluation mode
    encoder.eval()
    clf_head.eval()

    with torch.no_grad():
        for sentence in data:
            logits, gold_labels = process_sentence(
                sentence, label_to_i, tokeniser, encoder, clf_head,
                encoder_device, clf_head_device
            )
            pred_indices = logits.argmax(dim=-1).tolist()
            gold_indices = gold_labels.tolist()

            pred_labels = [i_to_label[i] for i in pred_indices]
            gold_labels = [i_to_label[i] for i in gold_indices]

            all_preds.append(pred_labels)
            all_golds.append(gold_labels)

    return all_preds, all_golds

def evaluate_predictions(preds, golds):
    # Track counts
    correct_by_label = Counter()
    predicted_by_label = Counter()
    gold_by_label = Counter()

    labelled_match_total = 0
    unlabelled_match_total = 0
    gold_total = 0

    for pred_seq, gold_seq in zip(preds, golds):
        pred_spans = extract_spans(pred_seq)
        gold_spans = extract_spans(gold_seq)

        pred_span_set = set(pred_spans)
        gold_span_set = set(gold_spans)

        pred_unlabelled = set((s, e) for s, e, _ in pred_spans)
        gold_unlabelled = set((s, e) for s, e, _ in gold_spans)

        labelled_match_total += len(pred_span_set & gold_span_set)
        unlabelled_match_total += len(pred_unlabelled & gold_unlabelled)
        gold_total += len(gold_spans)

        for s, e, label in pred_spans:
            predicted_by_label[label] += 1
        for s, e, label in gold_spans:
            gold_by_label[label] += 1
        for span in pred_span_set & gold_span_set:
            correct_by_label[span[2]] += 1

    # Span match scores
    labelled_score = labelled_match_total / gold_total if gold_total > 0 else 0
    unlabelled_score = unlabelled_match_total / gold_total if gold_total > 0 else 0

    # Per-label P/R/F1
    label_scores = {}
    for label in gold_by_label:
        tp = correct_by_label[label]
        pred = predicted_by_label[label]
        gold = gold_by_label[label]
        precision = tp / pred if pred > 0 else 0
        recall = tp / gold if gold > 0 else 0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
        label_scores[label] = {'precision': precision, 'recall': recall, 'f1': f1}

    # Macro-average
    macro_p = sum(score['precision'] for score in label_scores.values()) / len(label_scores)
    macro_r = sum(score['recall'] for score in label_scores.values()) / len(label_scores)
    macro_f1 = sum(score['f1'] for score in label_scores.values()) / len(label_scores)

    return labelled_score, unlabelled_score, label_scores, macro_p, macro_r, macro_f1

def run_full_tagset_evaluation(test_data, label_to_i, i_to_label, tokeniser, encoder, clf_head,
                               encoder_device, clf_head_device):
    preds, golds = get_predictions(test_data, label_to_i, i_to_label, tokeniser, encoder, clf_head,
                                   encoder_device, clf_head_device)

    # Evaluating
    labelled, unlabelled, label_scores, macro_p, macro_r, macro_f1 = evaluate_predictions(preds, golds)

    print(f"\nSpan Matching Scores:")
    print(f"  Labelled Match Score:   {labelled:.2f}")
    print(f"  Unlabelled Match Score: {unlabelled:.2f}\n")

    print("Per-label Precision, Recall, F1:")
    for label, scores in sorted(label_scores.items()):
        p, r, f1 = scores['precision'], scores['recall'], scores['f1']
        print(f"  {label:10s} | P: {p:.2f} | R: {r:.2f} | F1: {f1:.2f}")

    print(f"\nMacro-Averaged:")
    print(f"  Precision: {macro_p:.2f} | Recall: {macro_r:.2f} | F1: {macro_f1:.2f}")

#Testing on test set
run_full_tagset_evaluation(
    testing_data, label_to_i, i_to_label,
    tokeniser, encoder, clf_head,
    encoder_device, clf_head_device)

# Testing on OOD dataset
run_full_tagset_evaluation(
    OOD_testing_data, label_to_i, i_to_label,
    tokeniser, encoder, clf_head,
    encoder_device, clf_head_device)

# Error analysis

def extract_error_examples(test_data, all_preds, all_golds, max_examples=20):
    error_examples = []

    for i, (sentence, gold_seq, pred_seq) in enumerate(zip(test_data, all_golds, all_preds)):
        gold_spans = set(extract_spans(gold_seq))
        pred_spans = set(extract_spans(pred_seq))

        # Checking for any span mismatches (FP or FN)
        if gold_spans != pred_spans:
            words = [w for w, _ in sentence]
            fp = pred_spans - gold_spans  # predicted but incorrect
            fn = gold_spans - pred_spans  # missed
            error_examples.append({
                "index": i,
                "sentence": words,
                "gold_spans": list(gold_spans),
                "pred_spans": list(pred_spans),
                "false_positives": list(fp),
                "false_negatives": list(fn)
            })

    return error_examples[:max_examples]

all_preds, all_golds = get_predictions(testing_data, label_to_i, i_to_label, tokeniser, encoder, clf_head,
                                       encoder_device, clf_head_device)
error_cases = extract_error_examples(testing_data, all_preds, all_golds)

for case in error_cases:
    print(f"\nSentence #{case['index']}: {' '.join(case['sentence'])}")
    print(f"  Gold spans: {case['gold_spans']}")
    print(f"  Predicted spans: {case['pred_spans']}")
    print(f"  False Positives: {case['false_positives']}")
    print(f"  False Negatives: {case['false_negatives']}")
