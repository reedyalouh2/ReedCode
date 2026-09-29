use std::collections::HashMap;
use std::fs;
use std::io::{self, Write};

use anyhow::{Context, Result};
use dynamo_protocols::types::{
    ChatCompletionRequestAssistantMessage, ChatCompletionRequestAssistantMessageContent,
    ChatCompletionRequestMessage, CreateChatCompletionRequest,
};
use dynamo_renderer::{ChatTemplate, ContextMixins, OAIChatLikeRequest, OAIPromptFormatter, PromptFormatter};
use dynamo_tokenizers::{HuggingFaceTokenizer, traits::Encoder};
use minijinja::value::Value;
use serde::Deserialize;
use serde_json::json;

include!("upstream-request.rs");

#[derive(Deserialize)]
struct Case {
    name: String,
    original_request: CreateChatCompletionRequest,
    followup_request: CreateChatCompletionRequest,
    response_text: String,
    effective_normal_template_args: HashMap<String, serde_json::Value>,
}

#[derive(Deserialize)]
struct Input {
    config_path: String,
    tokenizer_path: String,
    cases: Vec<Case>,
}

struct NormalRequest<'a> {
    inner: &'a CreateChatCompletionRequest,
    template_args: &'a HashMap<String, serde_json::Value>,
}

impl OAIChatLikeRequest for NormalRequest<'_> {
    fn model(&self) -> String { self.inner.model() }
    fn messages(&self) -> Value { self.inner.messages() }
    fn typed_messages(&self) -> Option<&[ChatCompletionRequestMessage]> { self.inner.typed_messages() }
    fn tools(&self) -> Option<Value> { self.inner.tools() }
    fn tool_choice(&self) -> Option<Value> { self.inner.tool_choice() }
    fn response_format(&self) -> Option<Value> { self.inner.response_format() }
    fn reasoning_effort(&self) -> Option<Value> { self.inner.reasoning_effort() }
    fn should_add_generation_prompt(&self) -> bool { true }
    fn chat_template_args(&self) -> Option<&HashMap<String, serde_json::Value>> { Some(self.template_args) }
}

fn render_stock(
    formatter: &dyn OAIPromptFormatter,
    original_messages: Vec<ChatCompletionRequestMessage>,
    response_text: String,
) -> Result<(String, serde_json::Value)> {
    let assistant_msg =
        ChatCompletionRequestMessage::Assistant(ChatCompletionRequestAssistantMessage {
            content: Some(ChatCompletionRequestAssistantMessageContent::Text(
                response_text,
            )),
            ..Default::default()
        });

    let mut messages = original_messages;
    messages.push(assistant_msg);

    let prefill_request = SpeculativePrefillRequest::new(messages);
    let traits = json!({
        "tools": prefill_request.tools(),
        "chat_template_args": prefill_request.chat_template_args(),
        "add_generation_prompt": prefill_request.should_add_generation_prompt(),
        "messages": prefill_request.messages(),
    });
            let formatted_prompt = formatter.render(&prefill_request)?;
    Ok((formatted_prompt, traits))
}

fn main() -> Result<()> {
    let path = std::env::args().nth(1).context("Pass the input JSON path")?;
    let input: Input = serde_json::from_slice(&fs::read(path)?)?;
    let config: ChatTemplate = serde_json::from_slice(&fs::read(&input.config_path)?)?;
    let PromptFormatter::OAI(formatter) =
        PromptFormatter::from_parts(config, ContextMixins::default(), false)?;
    let tokenizer = HuggingFaceTokenizer::from_file(&input.tokenizer_path)?;
    let mut rows = Vec::new();
    for case in input.cases {
        let original = NormalRequest {
            inner: &case.original_request,
            template_args: &case.effective_normal_template_args,
        };
        let followup = NormalRequest {
            inner: &case.followup_request,
            template_args: &case.effective_normal_template_args,
        };
        let original_text = formatter.render(&original)?;
        let followup_text = formatter.render(&followup)?;
        let (prepared_text, stock_traits) = render_stock(
            formatter.as_ref(), case.original_request.messages.clone(), case.response_text,
        )?;
        rows.push(json!({
            "case": case.name,
            "original": {"text": original_text, "token_ids": tokenizer.encode(&original_text)?.token_ids()},
            "prepared": {"text": prepared_text, "token_ids": tokenizer.encode(&prepared_text)?.token_ids()},
            "followup": {"text": followup_text, "token_ids": tokenizer.encode(&followup_text)?.token_ids()},
            "stock_request_traits": stock_traits,
        }));
    }
    let mut output = io::BufWriter::new(io::stdout().lock());
    serde_json::to_writer_pretty(&mut output, &rows)?;
    writeln!(output)?;
    Ok(())
}
