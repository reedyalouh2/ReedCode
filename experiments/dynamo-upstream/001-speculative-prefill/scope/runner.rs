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
    id: String,
    config_path: String,
    tokenizer_path: String,
    template_path: Option<String>,
    original_request: CreateChatCompletionRequest,
    completed_assistant: ChatCompletionRequestMessage,
    followup_request: CreateChatCompletionRequest,
    response_text: String,
    settings: HashMap<String, serde_json::Value>,
}

struct Request<'a> {
    inner: &'a CreateChatCompletionRequest,
    settings: Option<&'a HashMap<String, serde_json::Value>>,
    generation_prompt: bool,
}

impl OAIChatLikeRequest for Request<'_> {
    fn model(&self) -> String { self.inner.model() }
    fn messages(&self) -> Value { self.inner.messages() }
    fn typed_messages(&self) -> Option<&[ChatCompletionRequestMessage]> { self.inner.typed_messages() }
    fn tools(&self) -> Option<Value> { self.inner.tools() }
    fn tool_choice(&self) -> Option<Value> { self.inner.tool_choice() }
    fn response_format(&self) -> Option<Value> { self.inner.response_format() }
    fn reasoning_effort(&self) -> Option<Value> { self.inner.reasoning_effort() }
    fn should_add_generation_prompt(&self) -> bool { self.generation_prompt }
    fn chat_template_args(&self) -> Option<&HashMap<String, serde_json::Value>> { self.settings }
}

struct SchemaRestored<'a> {
    stock: &'a SpeculativePrefillRequest,
    original: &'a CreateChatCompletionRequest,
    settings: Option<&'a HashMap<String, serde_json::Value>>,
}

impl OAIChatLikeRequest for SchemaRestored<'_> {
    fn model(&self) -> String { self.stock.model() }
    fn messages(&self) -> Value { self.stock.messages() }
    fn typed_messages(&self) -> Option<&[ChatCompletionRequestMessage]> { self.stock.typed_messages() }
    fn tools(&self) -> Option<Value> { self.original.tools() }
    fn should_add_generation_prompt(&self) -> bool { self.stock.should_add_generation_prompt() }
    fn chat_template_args(&self) -> Option<&HashMap<String, serde_json::Value>> { self.settings }
}

fn stock_request(original_messages: Vec<ChatCompletionRequestMessage>, response_text: String) -> SpeculativePrefillRequest {
// UPSTREAM_CONSTRUCTION
    prefill_request
}

fn stock_render(formatter: &dyn OAIPromptFormatter, prefill_request: SpeculativePrefillRequest) -> Result<String> {
// UPSTREAM_RENDER
    Ok(formatted_prompt)
}

fn outcome(rendered: Result<String>, tokenizer: &HuggingFaceTokenizer) -> serde_json::Value {
    match rendered.and_then(|text| Ok((tokenizer.encode(&text)?.token_ids().to_vec(), text))) {
        Ok((token_ids, text)) => json!({"status": "rendered", "text": text, "token_ids": token_ids}),
        Err(error) => json!({"status": "error", "error": format!("{error:#}")}),
    }
}

fn render(case: &Case) -> Result<serde_json::Value> {
    let mut config: serde_json::Value = serde_json::from_slice(&fs::read(&case.config_path)?)?;
    if let Some(path) = &case.template_path {
        config["chat_template"] = serde_json::Value::String(fs::read_to_string(path)?);
    }
    let config: ChatTemplate = serde_json::from_value(config)?;
    let PromptFormatter::OAI(formatter) =
        PromptFormatter::from_parts(config, ContextMixins::default(), false)?;
    let tokenizer = HuggingFaceTokenizer::from_file(&case.tokenizer_path)?;
    let request = |inner, generation_prompt| Request { inner, settings: Some(&case.settings), generation_prompt };
    let stock = stock_request(case.original_request.messages.clone(), case.response_text.clone());
    let schema_only = SchemaRestored { stock: &stock, original: &case.original_request, settings: None };
    let schema_settings = SchemaRestored { stock: &stock, original: &case.original_request, settings: Some(&case.settings) };
    let mut full_messages = case.original_request.messages.clone();
    full_messages.push(case.completed_assistant.clone());
    let full_stock = SpeculativePrefillRequest::new(full_messages);
    let full_assistant = SchemaRestored { stock: &full_stock, original: &case.original_request, settings: Some(&case.settings) };
    let mut renders = serde_json::Map::new();
    renders.insert("original".into(), outcome(formatter.render(&request(&case.original_request, true)), &tokenizer));
    renders.insert("followup".into(), outcome(formatter.render(&request(&case.followup_request, true)), &tokenizer));
    renders.insert("schema_restored".into(), outcome(formatter.render(&schema_only), &tokenizer));
    renders.insert("schema_and_settings_restored".into(), outcome(formatter.render(&schema_settings), &tokenizer));
    renders.insert("schema_settings_full_assistant".into(), outcome(formatter.render(&full_assistant), &tokenizer));
    let traits = json!({"tools": stock.tools(), "settings": stock.chat_template_args(), "generation_prompt": stock.should_add_generation_prompt()});
    renders.insert("stock".into(), outcome(stock_render(formatter.as_ref(), stock), &tokenizer));
    Ok(json!({"id": case.id, "renders": renders, "stock_traits": traits}))
}

fn main() -> Result<()> {
    let path = std::env::args().nth(1).context("Pass the input JSON path")?;
    let cases: Vec<Case> = serde_json::from_slice(&fs::read(path)?)?;
    let rows: Vec<_> = cases.iter().map(|case| match render(case) {
        Ok(row) => row,
        Err(error) => json!({"id": case.id, "setup_error": format!("{error:#}")}),
    }).collect();
    let mut output = io::BufWriter::new(io::stdout().lock());
    serde_json::to_writer_pretty(&mut output, &rows)?;
    writeln!(output)?;
    Ok(())
}
