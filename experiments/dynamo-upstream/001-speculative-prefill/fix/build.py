"""Build the CPU renderer patch and prepare a pinned Dynamo integration diff."""
import argparse
import difflib
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import os
import tarfile
import tomllib

ROOT = Path(__file__).resolve().parent
CURRENT = ROOT.parent / 'current-code'
REGISTRY = Path('/tmp/reedcode-renderer-cargo/registry/src/index.crates.io-1949cf8c6b5b557f')


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f'Unexpected source anchor: {old[:100]}')
    return text.replace(old, new, 1)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_diff(before, after, path):
    lines = difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                fromfile='a/'+path if before else '/dev/null', tofile='b/'+path)
    return ''.join(line if line.endswith('\n') else line+'\n\\ No newline at end of file\n'
                   for line in lines)


def patch_renderer(destination):
    original = REGISTRY / 'dynamo-renderer-5.4.0'
    shutil.copytree(original, destination, dirs_exist_ok=True)
    lib = destination / 'src/lib.rs'
    text = lib.read_text().replace('mod template;', 'mod template;\nmod speculative;')
    text = replace_once(text, "    fn render(&self, req: &dyn OAIChatLikeRequest) -> Result<String>;", '''    fn render(&self, req: &dyn OAIChatLikeRequest) -> Result<String>;

    /// A closed prefix for an unchanged conversation followed by tool results.
    /// Unsupported templates return None; callers must skip their warmup.
    fn render_speculative_tool_prefix(&self, _req: &dyn OAIChatLikeRequest) -> Result<Option<String>> {
        Ok(None)
    }''')
    lib.write_text(text)
    oai = destination / 'src/template/oai.rs'
    text = replace_once(oai.read_text(), 'impl OAIPromptFormatter for HfTokenizerConfigJsonFormatter {', '''impl OAIPromptFormatter for HfTokenizerConfigJsonFormatter {
    fn render_speculative_tool_prefix(&self, req: &dyn OAIChatLikeRequest) -> Result<Option<String>> {
        // Source identity, rather than a model name, binds the continuation contract.
        let tools = req.tools();
        let excluded = self.exclude_tools_when_tool_choice_none
            && req.tool_choice().is_some_and(|v| v.as_str() == Some("none"));
        let has_tools = !excluded && tools.as_ref().and_then(|v| v.len()).is_some_and(|n| n > 0);
        let name = if has_tools { "tool_use" } else { "default" };
        if self.env.get_template(name)?.source() != crate::speculative::QWEN3_TEMPLATE {
            return Ok(None);
        }
        crate::speculative::qwen_tool_prefix(self, req)
    }
''')
    oai.write_text(text)
    for name in ('speculative.rs', 'qwen3-tool-prefix.jinja'):
        shutil.copyfile(ROOT / 'renderer' / name, destination / 'src' / name)
    subprocess.run(['rustfmt','--edition','2024','--config','skip_children=true',str(lib),str(oai),str(destination/'src/speculative.rs')],check=True)
    paths = ['src/lib.rs', 'src/template/oai.rs', 'src/speculative.rs', 'src/qwen3-tool-prefix.jinja']
    diff = ''
    for path in paths:
        before = (original/path).read_text() if (original/path).exists() else ''
        diff += source_diff(before, (destination/path).read_text(), path)
    (ROOT/'renderer.patch').write_text(diff)


def patch_dynamo():
    source = CURRENT / 'sources/main/speculative_prefill.rs'
    original = source.read_text()
    text = original
    start = text.index("//! Speculative next-turn prefill")
    end = text.index("use std::pin::Pin;", start)
    text = text[:start] + "//! Warm supported tool-continuation prefixes after a complete assistant stream.\n//! Unknown rendering or tokenizer contracts skip optional preparation.\n\n" + text[end:]
    start = text.index("/// Optionally wraps")
    end = text.index("pub(super) fn maybe_wrap_stream", start)
    text = text[:start] + "/// Preserve the client stream while collecting one complete tool-call turn.\n/// Existing cancellation, admission and timeout bounds apply to preparation.\n" + text[end:]
    text = replace_once(text, 'use minijinja::value::Value;', '''#[path = "speculative_prefill_accumulator.rs"]
mod accumulator;
use accumulator::CompletedToolTurn;
#[path = "speculative_prefill_contract.rs"]
pub(super) mod contract;''')
    left = text.index('/// A minimal `OAIChatLikeRequest`')
    right = text.index('/// Preserve the client stream', left)
    text = text[:left] + text[right:]
    text = text.replace('ChatCompletionMessageContent, ChatCompletionRequestAssistantMessage,\n    ChatCompletionRequestAssistantMessageContent, ChatCompletionRequestMessage,',
                        'ChatCompletionRequestAssistantMessage, ChatCompletionRequestMessage,')
    text = text.replace('use dynamo_renderer::{OAIChatLikeRequest, OAIPromptFormatter};','use dynamo_renderer::OAIPromptFormatter;')
    text = replace_once(text, '    tasks: &PrefillTasks,\n)', '    tasks: &PrefillTasks,\n    cache_identity_supported: bool,\n)')
    text = replace_once(text, '    if !enabled {', '''    if !enabled || !cache_identity_supported
        || request.inner.n.unwrap_or(1) != 1
        || request.common.continue_final_message == Some(true) {
        if enabled {
            tracing::debug!(request_id, "Skipping speculative prefill: unsupported request identity or shape");
        }''')
    text = text.replace('tokio::sync::oneshot::channel::<String>()','tokio::sync::oneshot::channel::<ChatCompletionRequestAssistantMessage>()')
    text = replace_once(text, '    let messages = request.inner.messages.clone();', '    let render_request = request.clone();')
    text = text.replace('let response_text = tokio::select!', 'let assistant = tokio::select!')
    text = text.replace('Ok(response_text) => response_text,', 'Ok(assistant) => assistant,')
    text = replace_once(text, '            messages,\n            response_text,', '            render_request,\n            assistant,')
    left = text.index('    let mut accumulated_text = String::new();')
    right = text.index('\n}\n\n/// Signals downstream', left)
    text = text[:left] + '''    Box::pin(async_stream::stream! {
        let mut stream = stream;
        let mut completed = CompletedToolTurn::default();
        while let Some(item) = stream.next().await {
            if item.error.is_some() || item.event.as_deref() == Some("error") {
                completed.reject();
            }
            if let Some(response) = &item.data {
                completed.observe(&response.inner);
            }
            yield item;
        }
        // EOF rules out a later error or second choice before optional dispatch.
        if let Some(assistant) = completed.finish() {
            let _ = tx.send(assistant);
        }
    })''' + text[right:]
    text = replace_once(text, '    original_messages: Vec<ChatCompletionRequestMessage>,\n    response_text: String,',
                        '    mut render_request: NvCreateChatCompletionRequest,\n    assistant: ChatCompletionRequestAssistantMessage,')
    left = text.index('    let assistant_msg =')
    right = text.index('    let preprocessing_cancel =', left)
    text = text[:left] + '    let model = render_request.inner.model.clone();\n    render_request.inner.messages.push(ChatCompletionRequestMessage::Assistant(assistant));\n' + text[right:]
    text = replace_once(text, '            let formatted_prompt = formatter.render(&prefill_request)?;', '''            if !contract::supported_tokenizer(tokenizer.as_ref()) || preprocessing_cancel.is_cancelled() {
                tracing::debug!("Skipping speculative prefill: unsupported tokenizer contract");
                return Ok(None);
            }
            let Some(formatted_prompt) = formatter.render_speculative_tool_prefix(&render_request)? else {
                tracing::debug!("Skipping speculative prefill: unsupported continuation template");
                return Ok(None);
            };''')
    text = replace_once(text, '            Ok(Some(encoding.token_ids().to_vec()))', '''            let closed = tokenizer.encode("<|im_end|>")?;
            if closed.token_ids() != [151645] || encoding.token_ids().last() != Some(&151645) {
                tracing::debug!("Skipping speculative prefill: incompatible tokenizer boundary");
                return Ok(None);
            }
            Ok(Some(encoding.token_ids().to_vec()))''')
    text = replace_once(text, '.model("speculative_prefill".to_string())', '.model(model)')
    # Existing upstream async lifecycle tests need the new request and terminal fixture.
    production, tests = text.split('#[cfg(test)]', 1)
    (ROOT/'dynamo/speculative_prefill.rs').write_text(production)
    preprocessor = CURRENT/'sources/main/preprocessor.rs'
    before = preprocessor.read_text()
    before_for_patch = before
    before = replace_once(before, 'pub struct OpenAIPreprocessor {\n', 'pub struct OpenAIPreprocessor {\n    speculative_artifacts_supported: bool,\n')
    before = replace_once(before, '        let mdcsum = mdc.mdcsum().to_string();', '''        let speculative_artifacts_supported = speculative_prefill::contract::supported_artifacts(
            mdc.tokenizer.as_ref().map(|t| t.checksum()).as_deref(),
            mdc.prompt_formatter.as_ref().map(|t| t.checksum()).as_deref(),
            mdc.runtime_config.effective_tokenizer_backend().as_str(),
        );
        let mdcsum = mdc.mdcsum().to_string();''')
    before = replace_once(before, '            mdcsum,\n', '            mdcsum,\n            speculative_artifacts_supported,\n')
    after = replace_once(before, '        // Capture media counts before `common_request` is moved into the context.\n', '''        // This first continuation contract supports unnamespaced text models only.
        let speculative_identity_supported = self.speculative_artifacts_supported
            && request.nvext.as_ref().and_then(|n| n.agent_hints.as_ref()).and_then(|h| h.speculative_prefill) == Some(true)
            && !self.normalize_tool_call_args
            && request.nvext.as_ref().and_then(|n| n.use_raw_prompt) != Some(true)
            && request.nvext.as_ref().and_then(|n| n.token_data.as_ref()).is_none()
            && common_request.prompt_embeds.is_none()
            && common_request.multi_modal_data.is_none()
            && common_request.routing.as_ref().is_none_or(|r| {
                serde_json::to_value(r).ok() == serde_json::to_value(RoutingHints::default()).ok()
            });

        // Capture media counts before `common_request` is moved into the context.
''')
    after = replace_once(after, '            &self.speculative_prefill_tasks,\n',
                         '            &self.speculative_prefill_tasks,\n            speculative_identity_supported,\n')
    (ROOT/'dynamo/preprocessor.rs').write_text(after)
    return original, before_for_patch


def main(args):
    manifest = json.loads((CURRENT/'sources/manifest.json').read_text())
    for record in manifest['files']:
        if sha(CURRENT/'sources'/record['local_path']) != record['sha256']:
            raise ValueError('Pinned source changed: '+record['local_path'])
    registry = REGISTRY/'dynamo-renderer-5.4.0'
    package = REGISTRY.parent.parent/'cache'/REGISTRY.name/'dynamo-renderer-5.4.0.crate'
    lock = tomllib.loads((CURRENT/'sources/main/Cargo.lock').read_text())
    expected = next(p['checksum'] for p in lock['package'] if p['name']=='dynamo-renderer')
    if sha(package) != expected:
        raise ValueError('Registry renderer archive differs from upstream lock')
    with tarfile.open(package) as archive:
        for member in archive.getmembers():
            if member.isfile():
                name = Path(member.name).relative_to('dynamo-renderer-5.4.0')
                if (registry/name).read_bytes() != archive.extractfile(member).read():
                    raise ValueError('Registry renderer source changed: '+str(name))
    args.build_root.mkdir(parents=True, exist_ok=True)
    patch_renderer(args.build_root/'renderer')
    original, before = patch_dynamo()
    import adapt_lifecycle
    production = (ROOT/'dynamo/speculative_prefill.rs').read_text()
    tests = adapt_lifecycle.adapt()
    (ROOT/'dynamo/lifecycle-tests.rs').write_text(tests)
    (ROOT/'dynamo/speculative_prefill.rs').write_text(production + tests)
    subprocess.run(['rustfmt','--edition','2024','--config','skip_children=true',str(ROOT/'dynamo/speculative_prefill.rs'),str(ROOT/'dynamo/preprocessor.rs')],check=True)
    diff = ''
    for path, old, new in [
        ('lib/llm/src/preprocessor/speculative_prefill.rs', original, (ROOT/'dynamo/speculative_prefill.rs').read_text()),
        ('lib/llm/src/preprocessor.rs', before, (ROOT/'dynamo/preprocessor.rs').read_text()),
        ('lib/llm/src/preprocessor/speculative_prefill_accumulator.rs', '', (ROOT/'dynamo/accumulator.rs').read_text()),
        ('lib/llm/src/preprocessor/speculative_prefill_contract.rs', '', (ROOT/'dynamo/contract.rs').read_text()),
    ]:
        diff += source_diff(old, new, path)
    (ROOT/'dynamo.patch').write_text(diff)
    crate=args.build_root/'check'
    (crate/'src').mkdir(parents=True,exist_ok=True)
    manifest=(CURRENT/'compiled/main/Cargo.toml').read_text().replace('reedcode-stock-prefill-repro','reedcode-prefill-fix-check')
    manifest += '\n[patch.crates-io]\ndynamo-renderer = { path = "../renderer" }\n'
    (crate/'Cargo.toml').write_text(manifest)
    shutil.copyfile(CURRENT/'compiled/main/Cargo.lock',crate/'Cargo.lock')
    shutil.copyfile(ROOT.parents[2]/'dynamo-prefix/fixtures/tokenizer_config.json',crate/'config.json')
    for source,dest in [('check.rs','main.rs'),('dynamo/accumulator.rs','accumulator.rs'),('dynamo/contract.rs','contract.rs')]:
        shutil.copyfile(ROOT/source,crate/'src'/dest)
    env=dict(os.environ, RUSTC=str(args.rustc), CARGO_HOME='/tmp/reedcode-renderer-cargo', CARGO_TARGET_DIR='/tmp/reedcode-current-stock-prefill/target')
    result=subprocess.run([str(args.cargo),'test','--offline','--manifest-path',str(crate/'Cargo.toml')],env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    (ROOT/'cpu-tests.log').write_text(result.stdout)
    print(result.stdout[-12000:])
    if result.returncode: raise SystemExit(result.returncode)
    result=subprocess.run([str(args.cargo),'build','--offline','--manifest-path',str(crate/'Cargo.toml')],env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    (ROOT/'cpu-build.log').write_text(result.stdout)
    if result.returncode: print(result.stdout); raise SystemExit(result.returncode)
    shutil.copyfile(crate/'Cargo.lock',ROOT/'Cargo.lock')
    shutil.copyfile(crate/'Cargo.toml',ROOT/'Cargo.toml')
    shutil.copyfile(CURRENT/'sources/main/Cargo.lock',args.build_root/'renderer/Cargo.lock')
    result=subprocess.run([str(args.cargo),'test','--offline','--lib','--manifest-path',str(args.build_root/'renderer/Cargo.toml')],env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    (ROOT/'renderer-tests.log').write_text(result.stdout)
    if result.returncode: print(result.stdout); raise SystemExit(result.returncode)
    shutil.copyfile(args.build_root/'renderer/Cargo.lock',ROOT/'renderer-tests.lock')
    print(result.stdout[-220:])
    print(Path('/tmp/reedcode-current-stock-prefill/target/debug/reedcode-prefill-fix-check'))

if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--build-root',type=Path,default=Path('/tmp/reedcode-prefill-fix'))
    p.add_argument('--cargo',type=Path,default=Path('/tmp/reedcode-rustup/toolchains/1.96.1-aarch64-apple-darwin/bin/cargo'))
    p.add_argument('--rustc',type=Path,default=Path('/tmp/reedcode-rustup/toolchains/1.96.1-aarch64-apple-darwin/bin/rustc'))
    main(p.parse_args())
