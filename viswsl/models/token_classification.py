from typing import Any, Dict, List, Optional

import tokenizers as tkz
import torch
from torch import nn
from torch.nn import functional as F

from viswsl.data.structures import Batch
from viswsl.modules.textual_stream import TextualStream
from viswsl.modules.visual_stream import VisualStream


class TokenClassificationModel(nn.Module):
    def __init__(
        self,
        visual: VisualStream,
        textual: Optional[TextualStream] = None,
        visual_projection: Optional[str] = None,
        vocab_size: Optional[int] = None,
        ignore_indices: List[int] = [0, 1, 2, 3],
    ):
        # We do not use `textual_stream` or `visual_projection` but keep them
        # here for consistent call signature with other models.
        super().__init__()
        self.visual = visual
        self.ignore_indices = ignore_indices

        # Find the `vocab_size` (output size for classification). Use if provided
        # directly or get it from `textual_stream`.
        if vocab_size is not None:
            self.vocab_size = vocab_size
        elif textual is not None:
            self.vocab_size = textual.vocab_size
        else:
            raise ValueError("Need one of `textual` or `vocab_size`, found none.")

        # Linear layer to perform token classification using global average
        # pooled visual features.
        self.output = nn.Linear(self.visual.visual_feature_size, self.vocab_size)

    def forward(self, batch: Batch):

        # shape: (batch_size, visual_feature_size, ...)
        visual_features = self.visual(batch["image"])
        batch_size = visual_features.size(0)

        # Perform global avergae pooling of visual features.
        # shape: (batch_size, ..., visual_feature_size)
        visual_features = visual_features.view(
            batch_size, self.visual.visual_feature_size, -1
        ).permute(0, 2, 1)

        # shape: (batch_size, visual_feature_size)
        visual_features = visual_features.mean(dim=1)

        # Get logits and further log-probabilities.
        # shape: (batch_size, vocab_size)
        logits = self.output(visual_features)
        logprobs = F.log_softmax(logits, dim=1)

        # Average log-probs per unique token in associated caption to compute
        # loss. This is simply cross-entropy with target-vector as a K-hot
        # vector. Do in a for-loop, there isn't a straightforward vectorized way.
        loss = torch.tensor(0.0, device=logprobs.device)

        for index in range(batch_size):
            # Get unique tokens for particular instance.
            unique_tokens = batch["caption_tokens"][index].unique()

            # Ignore indices of special tokens such as [SOS], [EOS] etc. and
            # any other token specified.
            unique_tokens = [
                t for t in unique_tokens if t not in self.ignore_indices
            ]
            # Get log-probabilities corresponding to these tokens.
            instance_logprobs = logprobs[index, unique_tokens].mean()

            # Accumulate negative log-probability for this instance in loss.
            loss = loss - instance_logprobs

        # Average loss across instances.
        output_dict: Dict[str, Any] = {"loss": loss / batch_size}

        # Single scalar per batch for logging to tensorboard in training script.
        output_dict["loss_components"] = {
            "token_classification": loss.clone().detach() / batch_size
        }
        # Return top-10 tokens according to log-probabilities during validation.
        # Useful for logging.
        if not self.training:
            top_logprobs, top_tokens = logprobs.topk(k=10, dim=1)
            output_dict["predictions"] = top_tokens

        return output_dict

    def log_predictions(
        self, batch: Batch, tokenizer: tkz.implementations.BaseTokenizer
    ) -> str:

        self.eval()
        with torch.no_grad():
            predictions = self.forward(batch)["predictions"]
        self.train()

        predictions_str = ""
        for tokens, preds in zip(batch["caption_tokens"], predictions):
            # Predictions here are individual tokens, and do not have any order
            # like captions, so decode them separately so we don't strip off
            # metaspace character and special tokens if any.
            preds = [tokenizer.id_to_token(p) for p in preds.tolist()]
            predictions_str += f"""
                Caption tokens : {tokenizer.decode(tokens.tolist())}
                Predictions (f): {" ".join(preds)}

                """
        return predictions_str