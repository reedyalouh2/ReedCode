// SPDX-License-Identifier: Apache-2.0

use dynamo_protocols::types::{
    ChatCompletionRequestAssistantMessage, CreateChatCompletionStreamResponse,
};
use serde_json::{Value, json};
use std::collections::BTreeMap;

#[derive(Default)]
pub(super) struct CompletedToolTurn {
    content: Option<String>,
    reasoning: Option<String>,
    tools: BTreeMap<u32, Value>,
    finished: bool,
    failed: bool,
}

impl CompletedToolTurn {
    pub(super) fn reject(&mut self) {
        self.failed = true;
    }

    pub(super) fn observe(&mut self, response: &CreateChatCompletionStreamResponse) {
        // A usage-only frame is valid after the terminal choice.
        if response.choices.is_empty() {
            return;
        }
        if self.failed
            || self.finished
            || response.choices.len() != 1
            || response.choices[0].index != 0
        {
            self.reject();
            return;
        }
        let choice = &response.choices[0];
        let delta = serde_json::to_value(&choice.delta).expect("protocol delta is serializable");
        if delta.get("function_call").is_some_and(|v| !v.is_null())
            || delta.get("refusal").is_some_and(|v| !v.is_null())
            || delta
                .get("role")
                .is_some_and(|v| !v.is_null() && v != "assistant")
        {
            self.reject();
            return;
        }
        for (key, target) in [
            ("content", &mut self.content),
            ("reasoning_content", &mut self.reasoning),
        ] {
            if let Some(value) = delta.get(key).filter(|v| !v.is_null()) {
                if let Some(text) = value.as_str() {
                    target.get_or_insert_with(String::new).push_str(text);
                } else {
                    self.failed = true;
                    return;
                }
            }
        }
        if let Some(chunks) = &choice.delta.tool_calls {
            for chunk in chunks {
                let incoming = serde_json::to_value(chunk).expect("tool delta is serializable");
                let existing = self
                    .tools
                    .entry(chunk.index)
                    .or_insert_with(|| json!({"type":"function", "function":{"arguments":""}}));
                for key in ["id", "type"] {
                    if let Some(value) = incoming.get(key).filter(|v| !v.is_null()) {
                        if existing.get(key).is_some_and(|old| old != value) {
                            self.failed = true;
                            return;
                        }
                        existing[key] = value.clone();
                    }
                }
                if let Some(function) = incoming.get("function").filter(|v| !v.is_null()) {
                    if let Some(name) = function.get("name").filter(|v| !v.is_null()) {
                        if existing["function"]
                            .get("name")
                            .is_some_and(|old| old != name)
                        {
                            self.failed = true;
                            return;
                        }
                        existing["function"]["name"] = name.clone();
                    }
                    if let Some(args) = function.get("arguments").and_then(Value::as_str) {
                        let old = existing["function"]["arguments"].as_str().unwrap();
                        existing["function"]["arguments"] = Value::String(format!("{old}{args}"));
                    }
                }
            }
        }
        if let Some(reason) = choice.finish_reason {
            self.finished = true;
            if serde_json::to_value(reason).ok().as_ref() != Some(&json!("tool_calls")) {
                self.reject();
            }
        }
    }

    pub(super) fn finish(self) -> Option<ChatCompletionRequestAssistantMessage> {
        if self.failed || !self.finished || self.tools.is_empty() {
            return None;
        }
        let mut ids = std::collections::HashSet::new();
        for (expected, (index, tool)) in self.tools.iter().enumerate() {
            let id = tool["id"].as_str()?;
            let name = tool["function"]["name"].as_str()?;
            let args = tool["function"]["arguments"].as_str()?;
            if *index as usize != expected
                || id.is_empty()
                || name.is_empty()
                || !ids.insert(id)
                || tool["type"] != "function"
                || !serde_json::from_str::<Value>(args).ok()?.is_object()
            {
                return None;
            }
        }
        serde_json::from_value(
            json!({"content":self.content, "reasoning_content":self.reasoning,
            "tool_calls":self.tools.into_values().collect::<Vec<_>>()}),
        )
        .ok()
    }
}

#[cfg(test)]
mod tests {
    use super as accumulator;
    use super::*;
    use serde_json::Value as Json;
    fn chunk(delta: Json, reason: Json) -> CreateChatCompletionStreamResponse {
        serde_json::from_value(json!({"id":"x","model":"m","object":"chat.completion.chunk","created":0,"choices":[{"index":0,"delta":delta,"finish_reason":reason,"logprobs":null}]})).unwrap()
    }
    fn initial() -> Json {
        json!({"role":"assistant","tool_calls":[{"index":0,"id":"c1","type":"function","function":{"name":"read","arguments":"{"}}]})
    }
    fn ending() -> Json {
        json!({"tool_calls":[{"index":0,"function":{"arguments":"\"path\": \"x\"}"}}]})
    }

    #[test]
    fn fields_and_reasoning_survive_fragments() {
        let mut a = accumulator::CompletedToolTurn::default();
        a.observe(&chunk(initial(), Json::Null));
        a.observe(&chunk(
            json!({"content":"hello","reasoning_content":"think"}),
            Json::Null,
        ));
        a.observe(&chunk(ending(), json!("tool_calls")));
        let mut usage = chunk(json!({}), Json::Null);
        usage.choices.clear();
        a.observe(&usage);
        let result = serde_json::to_value(a.finish().unwrap()).unwrap();
        assert_eq!(
            result["tool_calls"][0]["function"]["arguments"],
            "{\"path\": \"x\"}"
        );
        assert_eq!(result["reasoning_content"], "think");
        assert_eq!(result["content"], "hello");
    }
    #[test]
    fn incomplete_or_limited_turns_skip() {
        for reason in [
            Json::Null,
            json!("length"),
            json!("stop"),
            json!("content_filter"),
        ] {
            let mut a = accumulator::CompletedToolTurn::default();
            a.observe(&chunk(initial(), Json::Null));
            a.observe(&chunk(ending(), reason));
            assert!(a.finish().is_none());
        }
    }
    #[test]
    fn malformed_conflicting_and_multiple_choices_skip() {
        for bad in [
            json!({"tool_calls":[{"index":0,"id":"different"}]}),
            json!({"tool_calls":[{"index":0,"function":{"name":"other"}}]}),
            json!({"content":[{"type":"text","text":"parts"}]}),
            json!({"refusal":"no"}),
            json!({"function_call":{"name":"legacy"}}),
        ] {
            let mut a = accumulator::CompletedToolTurn::default();
            a.observe(&chunk(initial(), Json::Null));
            a.observe(&chunk(bad, Json::Null));
            a.observe(&chunk(ending(), json!("tool_calls")));
            assert!(a.finish().is_none());
        }
        let mut c = chunk(initial(), Json::Null);
        c.choices.push(c.choices[0].clone());
        c.choices[1].index = 1;
        let mut a = accumulator::CompletedToolTurn::default();
        a.observe(&c);
        a.observe(&chunk(ending(), json!("tool_calls")));
        assert!(a.finish().is_none());
    }
    #[test]
    fn late_error_and_late_choice_skip() {
        for late_choice in [true, false] {
            let mut a = accumulator::CompletedToolTurn::default();
            a.observe(&chunk(initial(), Json::Null));
            a.observe(&chunk(ending(), json!("tool_calls")));
            if late_choice {
                a.observe(&chunk(json!({"content":"late"}), Json::Null));
            } else {
                a.reject();
            }
            assert!(a.finish().is_none());
        }
    }
    #[test]
    fn raw_tool_arguments_are_never_reserialized() {
        let args = "{ \"x\" : 123456789012345678901234567890 }";
        let mut d = initial();
        d["tool_calls"][0]["function"]["arguments"] = json!(args);
        let mut a = accumulator::CompletedToolTurn::default();
        a.observe(&chunk(d, json!("tool_calls")));
        let result = serde_json::to_value(a.finish().unwrap()).unwrap();
        assert_eq!(result["tool_calls"][0]["function"]["arguments"], args);
    }
    #[test]
    fn parallel_tools_are_ordered_and_unique() {
        let mut a = accumulator::CompletedToolTurn::default();
        a.observe(&chunk(json!({"tool_calls":[{"index":1,"id":"c2","type":"function","function":{"name":"read","arguments":"{}"}},{"index":0,"id":"c1","type":"function","function":{"name":"read","arguments":"{}"}}]}),json!("tool_calls")));
        let v = serde_json::to_value(a.finish().unwrap()).unwrap();
        assert_eq!(v["tool_calls"][0]["id"], "c1");
        assert_eq!(v["tool_calls"][1]["id"], "c2");
        for calls in [
            json!([{"index":1,"id":"c1","type":"function","function":{"name":"read","arguments":"{}"}}]),
            json!([{"index":0,"id":"c1","type":"function","function":{"name":"read","arguments":"{}"}},{"index":1,"id":"c1","type":"function","function":{"name":"read","arguments":"{}"}}]),
            json!([{"index":0,"id":"c1","type":"function","function":{"name":"read","arguments":"{broken"}}]),
        ] {
            let mut a = accumulator::CompletedToolTurn::default();
            a.observe(&chunk(json!({"tool_calls":calls}), json!("tool_calls")));
            assert!(a.finish().is_none());
        }
    }
}
