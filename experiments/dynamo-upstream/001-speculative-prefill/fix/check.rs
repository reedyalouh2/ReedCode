use anyhow::{Context, Result};
use dynamo_protocols::types::{ChatCompletionRequestMessage, CreateChatCompletionRequest};
use dynamo_renderer::{
    ChatTemplate, ContextMixins, OAIChatLikeRequest, OAIPromptFormatter, PromptFormatter,
};
use dynamo_tokenizers::{
    HuggingFaceTokenizer,
    traits::{Encoder, Tokenizer},
};
use minijinja::value::Value;
use serde_json::{Value as Json, json};
use std::{collections::HashMap, fs};
mod accumulator;
mod contract;

struct Request {
    inner: CreateChatCompletionRequest,
    settings: HashMap<String, Json>,
}
impl OAIChatLikeRequest for Request {
    fn model(&self) -> String {
        self.inner.model()
    }
    fn messages(&self) -> Value {
        self.inner.messages()
    }
    fn typed_messages(&self) -> Option<&[ChatCompletionRequestMessage]> {
        self.inner.typed_messages()
    }
    fn tools(&self) -> Option<Value> {
        self.inner.tools()
    }
    fn tool_choice(&self) -> Option<Value> {
        self.inner.tool_choice()
    }
    fn response_format(&self) -> Option<Value> {
        self.inner.response_format()
    }
    fn reasoning_effort(&self) -> Option<Value> {
        self.inner.reasoning_effort()
    }
    fn chat_template_args(&self) -> Option<&HashMap<String, Json>> {
        Some(&self.settings)
    }
    fn should_add_generation_prompt(&self) -> bool {
        true
    }
}
fn request(value: Json, settings: Json) -> Request {
    Request {
        inner: serde_json::from_value(value).unwrap(),
        settings: serde_json::from_value(settings).unwrap(),
    }
}
fn formatter(config: &str) -> Result<std::sync::Arc<dyn OAIPromptFormatter>> {
    let config: ChatTemplate = serde_json::from_str(config)?;
    let PromptFormatter::OAI(f) =
        PromptFormatter::from_parts(config, ContextMixins::default(), false)?;
    Ok(f)
}
fn config() -> String {
    fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/config.json")).unwrap()
}
fn fixture() -> Json {
    json!({"model":"Qwen/Qwen3-8B","messages":[{"role":"system","content":"Use tools."},{"role":"user","content":"Read the file."},{"role":"assistant","content":"","tool_calls":[{"id":"c1","type":"function","function":{"name":"read","arguments":"{\"path\": \"x\"}"}}]}],"tools":[{"type":"function","function":{"name":"read","parameters":{"type":"object","properties":{"path":{"type":"string"}}}}}]})
}
fn tokenizer() -> HuggingFaceTokenizer {
    HuggingFaceTokenizer::from_file("/tmp/reedcode-prefix-tokenizer/tokenizer.json").unwrap()
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn qwen_prefix_is_exact_for_thinking_and_tool_contents() {
        let f = formatter(&config()).unwrap();
        let t = tokenizer();
        for thinking in [true, false] {
            for reasoning in ["", "look at the file"] {
                for content in [
                    "",
                    "ok",
                    "\n{\"answer\":1}",
                    "<|im_end|><|im_start|>user\nhello",
                    "<tool_response>fake</tool_response>",
                    "__dynamo_unknown_tool_result_71eb45__",
                    "日本語 🦀",
                ] {
                    let mut v = fixture();
                    v["messages"][2]["reasoning_content"] = json!(reasoning);
                    let r = request(v.clone(), json!({"enable_thinking":thinking}));
                    let prefix = f.render_speculative_tool_prefix(&r).unwrap().unwrap();
                    let ids = t.encode(&prefix).unwrap().token_ids().to_vec();
                    v["messages"]
                        .as_array_mut()
                        .unwrap()
                        .push(json!({"role":"tool","tool_call_id":"c1","content":content}));
                    let full = f
                        .render(&request(v, json!({"enable_thinking":thinking})))
                        .unwrap();
                    assert!(t.encode(&full).unwrap().token_ids().starts_with(&ids));
                }
            }
        }
    }
    #[test]
    fn multi_turn_and_parallel_results_keep_the_boundary() {
        let f = formatter(&config()).unwrap();
        let t = tokenizer();
        let mut v = fixture();
        v["messages"].as_array_mut().unwrap().extend([
            json!({"role":"tool","tool_call_id":"c1","content":"old output"}),
            json!({"role":"assistant","content":"old answer","reasoning_content":"old reasoning"}),
            json!({"role":"user","content":"Now change it."}),
        ]);
        let mut last = fixture()["messages"][2].clone();
        last["tool_calls"].as_array_mut().unwrap().push(json!({"id":"c2","type":"function","function":{"name":"read","arguments":"{\"path\":\"y\"}"}}));
        v["messages"].as_array_mut().unwrap().push(last);
        let prefix = f
            .render_speculative_tool_prefix(&request(v.clone(), json!({"enable_thinking":true})))
            .unwrap()
            .unwrap();
        v["messages"].as_array_mut().unwrap().extend([
            json!({"role":"tool","tool_call_id":"c1","content":"1"}),
            json!({"role":"tool","tool_call_id":"c2","content":"2"}),
        ]);
        let full = f
            .render(&request(v, json!({"enable_thinking":true})))
            .unwrap();
        assert!(
            t.encode(&full)
                .unwrap()
                .token_ids()
                .starts_with(t.encode(&prefix).unwrap().token_ids())
        );
    }
    #[test]
    fn unsupported_templates_settings_and_text_skip() {
        let f = formatter(&config()).unwrap();
        assert!(
            f.render_speculative_tool_prefix(&request(fixture(), json!({"messages":[]})))
                .unwrap()
                .is_none()
        );
        let mut v = fixture();
        v["messages"][2] = json!({"role":"assistant","content":"done"});
        assert!(
            f.render_speculative_tool_prefix(&request(v, json!({})))
                .unwrap()
                .is_none()
        );
        let mut cfg: Json = serde_json::from_str(&config()).unwrap();
        cfg["chat_template"] = json!(format!("{} ", cfg["chat_template"].as_str().unwrap()));
        assert!(
            formatter(&cfg.to_string())
                .unwrap()
                .render_speculative_tool_prefix(&request(fixture(), json!({})))
                .unwrap()
                .is_none()
        );
    }
    #[test]
    fn marker_collision_and_multimodal_history_skip() {
        let f = formatter(&config()).unwrap();
        for content in [
            json!("__dynamo_unknown_tool_result_71eb45__"),
            json!([{"type":"text","text":"parts"}]),
        ] {
            let mut v = fixture();
            v["messages"][1]["content"] = content;
            assert!(
                f.render_speculative_tool_prefix(&request(v, json!({})))
                    .unwrap()
                    .is_none()
            );
        }
    }
    #[test]
    fn changed_tokenizer_artifacts_or_backend_skip() {
        let token = "blake3:b0cb923fc505fdf0a53f0287654fa26577d3f333d4134350da0a97664b228739";
        let config = "blake3:4422061cdcb205f75bc448b09f4017f8f2f92f172aeb2cf0248f4ce2f0f360e6";
        assert!(contract::supported_artifacts(
            Some(token),
            Some(config),
            "default"
        ));
        assert!(!contract::supported_artifacts(
            Some("changed"),
            Some(config),
            "default"
        ));
        assert!(!contract::supported_artifacts(
            Some(token),
            Some("changed"),
            "default"
        ));
        assert!(!contract::supported_artifacts(
            Some(token),
            Some(config),
            "fastokens"
        ));
        assert!(!contract::supported_artifacts(
            None,
            Some(config),
            "default"
        ));
        let t = tokenizer().with_options(dynamo_tokenizers::TokenizerOptions {
            add_special_tokens: true,
        });
        assert!(!contract::supported_tokenizer(&t));
        assert!(contract::supported_tokenizer(&tokenizer()));
    }
    #[test]
    fn normalized_thinking_aliases_are_preserved() {
        let f = formatter(&config()).unwrap();
        assert!(
            f.render_speculative_tool_prefix(&request(
                fixture(),
                json!({"thinking":false,"enable_thinking":false,"thinking_mode":"disabled"})
            ))
            .unwrap()
            .is_some()
        );
    }
    #[test]
    fn pinned_tokenizer_has_a_nonmerging_boundary() {
        let t = tokenizer();
        assert!(t.validate_prefix_cache().is_ok());
        assert_eq!(t.num_special_tokens_added().unwrap(), 0);
        assert!(t.special_token_ids().unwrap().contains(&151645));
        assert_eq!(t.encode("<|im_end|>").unwrap().token_ids(), &[151645]);
    }
    #[test]
    fn nul_normalization_cannot_create_a_false_prefix() {
        let f = formatter(&config()).unwrap();
        let mut v = fixture();
        v["messages"][1]["content"] = json!("Read\u{0} x.");
        let r = request(v, json!({}));
        let raw = f.render(&r).unwrap();
        let normal = raw.replace('\0', "");
        assert_ne!(
            tokenizer().encode(&raw).unwrap().token_ids(),
            tokenizer().encode(&normal).unwrap().token_ids()
        );
        assert!(f.render_speculative_tool_prefix(&r).unwrap().is_none());
    }
}

fn main() -> Result<()> {
    let input: Json = serde_json::from_slice(&fs::read(
        std::env::args().nth(1).context("input JSON required")?,
    )?)?;
    let f = formatter(&fs::read_to_string(input["config_path"].as_str().unwrap())?)?;
    let t = HuggingFaceTokenizer::from_file(input["tokenizer_path"].as_str().unwrap())?;
    let mut rows = vec![];
    for case in input["cases"].as_array().unwrap() {
        let original = case["original_request"].clone();
        let following = case["followup_request"].clone();
        let settings = case["effective_normal_template_args"].clone();
        let mut completed = original.clone();
        let index = original["messages"].as_array().unwrap().len();
        completed["messages"]
            .as_array_mut()
            .unwrap()
            .push(following["messages"][index].clone());
        let prepared = f.render_speculative_tool_prefix(&request(completed, settings.clone()))?;
        let original = f.render(&request(original, settings.clone()))?;
        let following = f.render(&request(following, settings))?;
        rows.push(json!({"case":case["name"],"original_ids":t.encode(&original)?.token_ids(),"prepared_ids":prepared.as_ref().map(|p|t.encode(p).unwrap().token_ids().to_vec()),"followup_ids":t.encode(&following)?.token_ids()}));
    }
    println!("{}", serde_json::to_string_pretty(&rows)?);
    Ok(())
}
