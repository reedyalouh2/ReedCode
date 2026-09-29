use std::collections::HashMap;
use std::fs;
use std::io::{self, Write};

use anyhow::{Context, Result};
use dynamo_protocols::types::{ChatCompletionRequestMessage, CreateChatCompletionRequest};
use dynamo_renderer::{ChatTemplate, ContextMixins, OAIChatLikeRequest, PromptFormatter};
use minijinja::value::Value;
use serde::Deserialize;
use serde_json::json;

#[derive(Deserialize)]
struct Case {
    id: String,
    config_path: String,
    request: serde_json::Value,
    #[serde(default)]
    template_args: HashMap<String, serde_json::Value>,
}

struct Request {
    inner: CreateChatCompletionRequest,
    template_args: HashMap<String, serde_json::Value>,
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

    fn should_add_generation_prompt(&self) -> bool {
        true
    }

    fn chat_template_args(&self) -> Option<&HashMap<String, serde_json::Value>> {
        Some(&self.template_args)
    }
}

fn render(case: &Case) -> Result<String> {
    let config: ChatTemplate = serde_json::from_slice(&fs::read(&case.config_path)?)?;
    let PromptFormatter::OAI(formatter) =
        PromptFormatter::from_parts(config, ContextMixins::default(), false)?;
    let request = Request {
        inner: serde_json::from_value(case.request.clone())?,
        template_args: case.template_args.clone(),
    };
    formatter.render(&request)
}

fn main() -> Result<()> {
    let path = std::env::args()
        .nth(1)
        .context("Pass a JSON array of rendering cases")?;
    let cases: Vec<Case> = serde_json::from_slice(&fs::read(path)?)?;
    let rows: Vec<_> = cases
        .iter()
        .map(|case| match render(case) {
            Ok(text) => json!({"id": case.id, "status": "rendered", "text": text}),
            Err(error) => json!({"id": case.id, "status": "error", "error": format!("{error:#}")}),
        })
        .collect();
    let mut output = io::BufWriter::new(io::stdout().lock());
    serde_json::to_writer_pretty(&mut output, &rows)?;
    writeln!(output)?;
    Ok(())
}
