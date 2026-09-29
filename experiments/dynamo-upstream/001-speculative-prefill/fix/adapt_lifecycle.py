"""Adapt the pinned upstream lifecycle tests to completed tool continuations."""

import hashlib
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parent / "current-code/sources/main/speculative_prefill.rs"


def replace_exact(text, old, new, count=1):
    if text.count(old) != count:
        raise ValueError(f"Unexpected lifecycle-test anchor count: {old[:80]}")
    return text.replace(old, new)


MODEL_PARTS = '''    // Lifecycle tests use a supported boundary without loading a model fixture.
    // Renderer and tokenizer parity are covered by the separate CPU regressions.
    struct LifecycleFormatter;

    impl OAIPromptFormatter for LifecycleFormatter {
        fn supports_add_generation_prompt(&self) -> bool { true }

        fn render(&self, request: &dyn OAIChatLikeRequest) -> Result<String> {
            let messages = request.typed_messages().expect("typed fixture messages");
            let Some(ChatCompletionRequestMessage::Assistant(assistant)) = messages.last() else {
                anyhow::bail!("lifecycle fixture must end with its completed assistant");
            };
            assert!(assistant.tool_calls.as_ref().is_some_and(|calls| !calls.is_empty()));
            Ok("fixture prefix<|im_end|>".to_string())
        }

        fn render_speculative_tool_prefix(&self, request: &dyn OAIChatLikeRequest) -> Result<Option<String>> {
            self.render(request).map(Some)
        }
    }

    struct LifecycleTokenizer;

    impl Encoder for LifecycleTokenizer {
        fn encode(&self, input: &str) -> Result<Encoding> {
            match input {
                "<|im_end|>" => Ok(Encoding::Sp(vec![151645])),
                "fixture prefix<|im_end|>" => Ok(Encoding::Sp(vec![42, 43, 151645])),
                _ => anyhow::bail!("unexpected lifecycle fixture input"),
            }
        }

        fn encode_batch(&self, inputs: &[&str]) -> Result<Vec<Encoding>> {
            inputs.iter().map(|input| self.encode(input)).collect()
        }
    }

    impl Decoder for LifecycleTokenizer {
        fn decode(&self, _token_ids: &[u32], _skip_special_tokens: bool) -> Result<DecodeResult> {
            Ok(DecodeResult::Complete("fixture prefix<|im_end|>".to_string()))
        }
    }

    impl Tokenizer for LifecycleTokenizer {
        fn validate_prefix_cache(&self) -> Result<()> { Ok(()) }
        fn token_to_id(&self, token: &str) -> Result<Option<u32>> {
            Ok((token == "<|im_end|>").then_some(151645))
        }
        fn special_token_ids(&self) -> Result<Vec<u32>> { Ok(vec![151645]) }
        fn num_special_tokens_added(&self) -> Result<usize> { Ok(0) }
    }

    fn sample_model_parts() -> (Arc<dyn OAIPromptFormatter>, Arc<dyn Tokenizer>) {
        (Arc::new(LifecycleFormatter), Arc::new(LifecycleTokenizer))
    }

    fn completed_assistant() -> ChatCompletionRequestAssistantMessage {
        serde_json::from_value(serde_json::json!({
            "content": "8849 m tall.",
            "tool_calls": [{"id": "call-height", "type": "function",
                "function": {"name": "lookup_height", "arguments": "{}"}}]
        })).unwrap()
    }

'''


def adapt():
    original = SOURCE.read_text()
    _, source_tests = original.split("#[cfg(test)]", 1)
    text = "#[cfg(test)]" + source_tests
    text = replace_exact(text, "    use std::path::PathBuf;\n", "")
    text = replace_exact(text, "        ChatChoiceStream, ChatCompletionRequestUserMessage,", "        ChatChoiceStream, ChatCompletionMessageContent, ChatCompletionRequestUserMessage,")
    text = replace_exact(text, "    use dynamo_renderer::PromptFormatter;", "    use dynamo_renderer::OAIChatLikeRequest;")
    text = replace_exact(text, "    use crate::model_card::ModelDeploymentCard;\n    use crate::preprocessor::prompt::prompt_formatter_from_mdc;", "    use crate::tokenizers::traits::{Decoder, Encoder};\n    use crate::tokenizers::{DecodeResult, Encoding};")
    start = text.index("    fn sample_model_parts()")
    end = text.index("    fn chat_request(", start)
    text = text[:start] + MODEL_PARTS + text[end:]
    text = replace_exact(text, '            inner: CreateChatCompletionRequest {\n                model: "mock-llama".to_string(),', '''            inner: CreateChatCompletionRequest {
                model: "lifecycle-fixture".to_string(),
                tools: Some(serde_json::from_value(serde_json::json!([
                    {"type": "function", "function": {"name": "lookup_height",
                        "parameters": {"type": "object", "properties": {}}}}
                ])).unwrap()),''')
    text = replace_exact(text, '                tool_calls: None,', '''                tool_calls: finish.then(|| serde_json::from_value(serde_json::json!([
                    {"index": 0, "id": "call-height", "type": "function",
                        "function": {"name": "lookup_height", "arguments": "{}"}}
                ])).unwrap()),''')
    text = replace_exact(text, 'finish.then_some(FinishReason::Stop)', 'finish.then_some(FinishReason::ToolCalls)')
    text = replace_exact(text, '                model: "mock-llama".to_string(),', '                model: "lifecycle-fixture".to_string(),')
    text = replace_exact(text, '''                chat_request(true).inner.messages,
                "8849 m tall.".to_string(),''', '''                chat_request(true),
                completed_assistant(),''', count=2)
    text = replace_exact(text, '''                self.entered.lock().take().unwrap().send(()).unwrap();
                self.release.lock().recv_timeout(Duration::from_secs(10))?;
                self.inner.encode(input)''', '''                if input != "<|im_end|>" {
                    if let Some(entered) = self.entered.lock().take() {
                        entered.send(()).unwrap();
                        self.release.lock().recv_timeout(Duration::from_secs(10))?;
                    }
                }
                self.inner.encode(input)''')
    text = replace_exact(text, '''        impl Tokenizer for BlockingTokenizer {
            fn validate_prefix_cache(&self) -> Result<()> {
                self.inner.validate_prefix_cache()
            }
        }''', '''        impl Tokenizer for BlockingTokenizer {
            fn validate_prefix_cache(&self) -> Result<()> {
                self.inner.validate_prefix_cache()
            }
            fn token_to_id(&self, token: &str) -> Result<Option<u32>> {
                self.inner.token_to_id(token)
            }
            fn special_token_ids(&self) -> Result<Vec<u32>> {
                self.inner.special_token_ids()
            }
            fn num_special_tokens_added(&self) -> Result<usize> {
                self.inner.num_special_tokens_added()
            }
        }''')
    formatter_hook = '''            fn render_speculative_tool_prefix(&self, request: &dyn OAIChatLikeRequest) -> Result<Option<String>> {
                self.render(request).map(Some)
            }

'''
    text = replace_exact(text, '            fn render(&self, request: &dyn OAIChatLikeRequest) -> Result<String> {', formatter_hook + '            fn render(&self, request: &dyn OAIChatLikeRequest) -> Result<String> {', count=2)
    # Restrict the extra argument to the final owner argument in wrapper calls.
    owner_argument = re.compile(r"(?m)^(\s*)&(tasks|replacement),\n(\s*)\);")
    text, changed = owner_argument.subn(r"\1&\2,\n\1true,\n\3);", text)
    if changed != 12:
        raise ValueError(f"Expected 12 wrapper calls, found {changed}")
    names = lambda source: re.findall(r"#\[(?:tokio::)?test(?:\([^\n]*\))?\]\s*(?:async )?fn (\w+)", source)
    if names(text) != names(source_tests):
        raise ValueError("An upstream lifecycle test was dropped or renamed")
    header = ("// Adapted from pinned Dynamo main for completed tool continuations.\n"
              "// Original test names and assertions are retained with tool-call fixtures.\n"
              f"// Source SHA256: {hashlib.sha256(original.encode()).hexdigest()}\n\n")
    return header + text


if __name__ == "__main__":
    target = ROOT / "dynamo/lifecycle-tests.rs"
    target.write_text(adapt())
    print(target)
