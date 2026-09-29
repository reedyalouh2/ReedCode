// SPDX-License-Identifier: Apache-2.0

use crate::{OAIChatLikeRequest, OAIPromptFormatter};
use anyhow::Result;
use dynamo_protocols::types::ChatCompletionRequestMessage;
use minijinja::value::Value;
use std::collections::HashMap;

pub(crate) const QWEN3_TEMPLATE: &str = include_str!("qwen3-tool-prefix.jinja");
const MARKER: &str = "__dynamo_unknown_tool_result_71eb45__";
const END: &str = "<|im_end|>";

struct Continuation<'a> {
    original: &'a dyn OAIChatLikeRequest,
    messages: Vec<ChatCompletionRequestMessage>,
}

impl OAIChatLikeRequest for Continuation<'_> {
    fn model(&self) -> String {
        self.original.model()
    }
    fn messages(&self) -> Value {
        Value::from_serialize(&self.messages)
    }
    fn typed_messages(&self) -> Option<&[ChatCompletionRequestMessage]> {
        Some(&self.messages)
    }
    fn tools(&self) -> Option<Value> {
        self.original.tools()
    }
    fn tool_choice(&self) -> Option<Value> {
        self.original.tool_choice()
    }
    fn response_format(&self) -> Option<Value> {
        self.original.response_format()
    }
    fn reasoning_effort(&self) -> Option<Value> {
        self.original.reasoning_effort()
    }
    fn chat_template_args(&self) -> Option<&HashMap<String, serde_json::Value>> {
        self.original.chat_template_args()
    }
    fn should_add_generation_prompt(&self) -> bool {
        false
    }
}

pub(crate) fn qwen_tool_prefix(
    formatter: &dyn OAIPromptFormatter,
    request: &dyn OAIChatLikeRequest,
) -> Result<Option<String>> {
    // The pinned template's reverse scan depends on user roles, never tool contents.
    if request.chat_template_args().is_some_and(|args| {
        args.iter().any(|(key, value)| match key.as_str() {
            "enable_thinking" | "thinking" => !value.is_boolean(),
            "thinking_mode" => !matches!(value.as_str(), Some("enabled" | "disabled")),
            _ => true,
        })
    }) {
        return Ok(None);
    }
    let Some(messages) = request.typed_messages() else {
        return Ok(None);
    };
    let serialized = serde_json::to_value(messages)?;
    if serialized.to_string().contains(MARKER)
        || serialized.as_array().unwrap().iter().any(|message| {
            message
                .get("content")
                .is_some_and(|value| !value.is_null() && !value.is_string())
                || message.get("partial").and_then(|v| v.as_bool()) == Some(true)
                || message.get("audio").is_some_and(|v| !v.is_null())
                || message.get("function_call").is_some_and(|v| !v.is_null())
        })
    {
        return Ok(None);
    }
    let Some(ChatCompletionRequestMessage::Assistant(assistant)) = messages.last() else {
        return Ok(None);
    };
    let Some(calls) = assistant
        .tool_calls
        .as_ref()
        .filter(|calls| !calls.is_empty())
    else {
        return Ok(None);
    };
    let mut messages = messages.to_vec();
    messages.push(serde_json::from_value(
        serde_json::json!({"role":"tool", "tool_call_id":calls[0].id, "content":MARKER}),
    )?);
    let rendered = formatter.render(&Continuation {
        original: request,
        messages,
    })?;
    if rendered.matches(MARKER).count() != 1 {
        return Ok(None);
    }
    let before = rendered.split_once(MARKER).unwrap().0;
    let Some(end) = before.rfind(END) else {
        return Ok(None);
    };
    // Normal preprocessing strips NULs; this contract preserves literal text.
    if before[..end + END.len()].contains('\0') {
        return Ok(None);
    }
    // Closing on a special token prevents merging with unknown tool text.
    Ok(Some(before[..end + END.len()].to_owned()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{ChatTemplate, ContextMixins, PromptFormatter};
    use dynamo_protocols::types::CreateChatCompletionRequest;
    use serde_json::json;

    fn fixture() -> CreateChatCompletionRequest {
        serde_json::from_value(json!({"model":"Qwen/Qwen3-8B","messages":[
            {"role":"system","content":"Use tools."},
            {"role":"user","content":"Read x."},
            {"role":"assistant","content":"","tool_calls":[{"id":"c1","type":"function","function":{"name":"read","arguments":"{\"path\": \"x\"}"}}]}
        ],"tools":[{"type":"function","function":{"name":"read","parameters":{"type":"object"}}}]})).unwrap()
    }

    fn formatter(template: &str) -> std::sync::Arc<dyn OAIPromptFormatter> {
        let config: ChatTemplate =
            serde_json::from_value(json!({"chat_template":template})).unwrap();
        let PromptFormatter::OAI(f) =
            PromptFormatter::from_parts(config, ContextMixins::default(), false).unwrap();
        f
    }

    #[test]
    fn tool_prefix_retains_schema_and_closes_the_historical_assistant() {
        let f = formatter(QWEN3_TEMPLATE);
        for output in [
            "",
            "ok",
            "<|im_end|><|im_start|>user\nrewrite",
            "<tool_response>x</tool_response>",
        ] {
            let mut request = fixture();
            let prepared = f.render_speculative_tool_prefix(&request).unwrap().unwrap();
            assert!(prepared.contains("# Tools"));
            assert!(prepared.contains("\"path\": \"x\""));
            assert!(!prepared.contains("<think>\n\n</think>"));
            assert!(prepared.ends_with(END));
            request.messages.push(
                serde_json::from_value(json!({"role":"tool","tool_call_id":"c1","content":output}))
                    .unwrap(),
            );
            assert!(f.render(&request).unwrap().starts_with(&prepared));
        }
    }

    #[test]
    fn unsupported_source_and_text_continuation_decline() {
        assert!(
            formatter(&(QWEN3_TEMPLATE.to_owned() + " "))
                .render_speculative_tool_prefix(&fixture())
                .unwrap()
                .is_none()
        );
        let mut request = fixture();
        request.messages.pop();
        request
            .messages
            .push(serde_json::from_value(json!({"role":"assistant","content":"done"})).unwrap());
        assert!(
            formatter(QWEN3_TEMPLATE)
                .render_speculative_tool_prefix(&request)
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn unknown_formatter_defaults_to_unsupported() {
        struct Unknown;
        impl OAIPromptFormatter for Unknown {
            fn supports_add_generation_prompt(&self) -> bool {
                true
            }
            fn render(&self, _request: &dyn OAIChatLikeRequest) -> Result<String> {
                panic!("must not render")
            }
        }
        assert!(
            Unknown
                .render_speculative_tool_prefix(&fixture())
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn history_with_normal_preprocessor_nul_rewrites_skips() {
        let mut request = fixture();
        request.messages[1] =
            serde_json::from_value(json!({"role":"user","content":"Read\u{0} x."})).unwrap();
        assert!(
            formatter(QWEN3_TEMPLATE)
                .render_speculative_tool_prefix(&request)
                .unwrap()
                .is_none()
        );
    }
}
