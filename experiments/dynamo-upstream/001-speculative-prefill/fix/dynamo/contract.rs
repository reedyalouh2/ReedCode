// SPDX-License-Identifier: Apache-2.0
use dynamo_renderer::dynamo_tokenizers::traits::Tokenizer;

pub(crate) fn supported_artifacts(
    tokenizer: Option<&str>,
    config: Option<&str>,
    backend: &str,
) -> bool {
    tokenizer == Some("blake3:b0cb923fc505fdf0a53f0287654fa26577d3f333d4134350da0a97664b228739")
        && config == Some("blake3:4422061cdcb205f75bc448b09f4017f8f2f92f172aeb2cf0248f4ce2f0f360e6")
        && backend == "default"
}

pub(super) fn supported_tokenizer(tokenizer: &dyn Tokenizer) -> bool {
    tokenizer.validate_prefix_cache().is_ok()
        && tokenizer.num_special_tokens_added().ok() == Some(0)
        && tokenizer.token_to_id("<|im_end|>").ok().flatten() == Some(151645)
        && tokenizer
            .special_token_ids()
            .is_ok_and(|ids| ids.contains(&151645))
        && tokenizer
            .encode("<|im_end|>")
            .is_ok_and(|e| e.token_ids() == [151645])
}
