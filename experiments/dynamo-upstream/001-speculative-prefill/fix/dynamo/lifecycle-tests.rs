// Adapted from pinned Dynamo main for completed tool continuations.
// Original test names and assertions are retained with tool-call fixtures.
// Source SHA256: d1ea24631f4d7c9e731615948bc9a128afdd70a4326bad87cf1569200b1535fb

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    use std::task::Poll;

    use async_trait::async_trait;
    use dynamo_protocols::types::{
        ChatChoiceStream, ChatCompletionMessageContent, ChatCompletionRequestUserMessage,
        ChatCompletionRequestUserMessageContent, ChatCompletionStreamResponseDelta,
        CreateChatCompletionRequest, CreateChatCompletionStreamResponse, FinishReason,
    };
    use dynamo_renderer::OAIChatLikeRequest;
    use dynamo_runtime::pipeline::ResponseStream;
    use tokio::time::Instant;

    use crate::tokenizers::traits::{Decoder, Encoder};
    use crate::tokenizers::{DecodeResult, Encoding};
    use crate::protocols::common::extensions::{AgentHints, NvExt};

    use super::*;

    type BackendEngine =
        dyn AsyncEngine<SingleIn<PreprocessedRequest>, ManyOut<Annotated<BackendOutput>>, Error>;

    struct DropProbe(Arc<AtomicBool>);

    impl Drop for DropProbe {
        fn drop(&mut self) {
            self.0.store(true, Ordering::SeqCst);
        }
    }

    #[derive(Debug, Default)]
    struct StallingBackend {
        generate_calls: AtomicUsize,
        dispatched: tokio::sync::Notify,
        stream_dropped: Arc<AtomicBool>,
        context: Mutex<Option<Arc<dyn AsyncEngineContext>>>,
    }

    impl StallingBackend {
        fn generate_calls(&self) -> usize {
            self.generate_calls.load(Ordering::SeqCst)
        }

        fn stream_dropped(&self) -> bool {
            self.stream_dropped.load(Ordering::SeqCst)
        }

        fn was_asked_to_stop(&self) -> bool {
            self.context
                .lock()
                .as_ref()
                .map(|ctx| ctx.is_stopped())
                .unwrap_or(false)
        }
    }

    #[async_trait]
    impl AsyncEngine<SingleIn<PreprocessedRequest>, ManyOut<Annotated<BackendOutput>>, Error>
        for StallingBackend
    {
        async fn generate(
            &self,
            request: SingleIn<PreprocessedRequest>,
        ) -> Result<ManyOut<Annotated<BackendOutput>>, Error> {
            self.generate_calls.fetch_add(1, Ordering::SeqCst);
            let (_request, context) = request.transfer(());
            let ctx = context.context();
            *self.context.lock() = Some(ctx.clone());
            self.dispatched.notify_one();

            let probe = DropProbe(self.stream_dropped.clone());
            let stalled = futures::stream::poll_fn(move |_cx| {
                let _ = &probe;
                Poll::Pending
            });

            Ok(ResponseStream::new(Box::pin(stalled), ctx))
        }
    }

    #[derive(Debug, Default)]
    struct WindDownBackend {
        generate_calls: AtomicUsize,
        dispatched: tokio::sync::Notify,
        stream_dropped: Arc<AtomicBool>,
        cleanup_ran: Arc<AtomicBool>,
        context: Mutex<Option<Arc<dyn AsyncEngineContext>>>,
    }

    impl WindDownBackend {
        fn stream_dropped(&self) -> bool {
            self.stream_dropped.load(Ordering::SeqCst)
        }

        fn cleanup_ran(&self) -> bool {
            self.cleanup_ran.load(Ordering::SeqCst)
        }

        fn was_asked_to_stop(&self) -> bool {
            self.context
                .lock()
                .as_ref()
                .map(|ctx| ctx.is_stopped())
                .unwrap_or(false)
        }
    }

    #[async_trait]
    impl AsyncEngine<SingleIn<PreprocessedRequest>, ManyOut<Annotated<BackendOutput>>, Error>
        for WindDownBackend
    {
        async fn generate(
            &self,
            request: SingleIn<PreprocessedRequest>,
        ) -> Result<ManyOut<Annotated<BackendOutput>>, Error> {
            self.generate_calls.fetch_add(1, Ordering::SeqCst);
            let (_request, context) = request.transfer(());
            let ctx = context.context();
            *self.context.lock() = Some(ctx.clone());

            let probe = DropProbe(self.stream_dropped.clone());
            let cleanup = self.cleanup_ran.clone();
            let watched = ctx.clone();
            let winding =
                futures::stream::unfold(Some((watched, cleanup, probe)), |state| async move {
                    let (watched, cleanup, probe) = state?;
                    let _ = &probe;
                    watched.stopped().await;
                    cleanup.store(true, Ordering::SeqCst);
                    None::<(Annotated<BackendOutput>, _)>
                });

            self.dispatched.notify_one();
            Ok(ResponseStream::new(Box::pin(winding), ctx))
        }
    }

    #[derive(Debug, Default)]
    struct StalledInGenerateBackend {
        generate_calls: AtomicUsize,
        dispatched: tokio::sync::Notify,
        context: Mutex<Option<Arc<dyn AsyncEngineContext>>>,
    }

    impl StalledInGenerateBackend {
        fn generate_calls(&self) -> usize {
            self.generate_calls.load(Ordering::SeqCst)
        }

        fn was_asked_to_stop(&self) -> bool {
            self.context
                .lock()
                .as_ref()
                .map(|ctx| ctx.is_stopped())
                .unwrap_or(false)
        }
    }

    #[async_trait]
    impl AsyncEngine<SingleIn<PreprocessedRequest>, ManyOut<Annotated<BackendOutput>>, Error>
        for StalledInGenerateBackend
    {
        async fn generate(
            &self,
            request: SingleIn<PreprocessedRequest>,
        ) -> Result<ManyOut<Annotated<BackendOutput>>, Error> {
            self.generate_calls.fetch_add(1, Ordering::SeqCst);
            let (_request, context) = request.transfer(());
            let ctx = context.context();
            *self.context.lock() = Some(ctx.clone());

            self.dispatched.notify_one();
            ctx.stopped().await;
            Err(Error::msg("cancelled while selecting a worker"))
        }
    }

    // Lifecycle tests use a supported boundary without loading a model fixture.
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

    fn chat_request(speculative_prefill: bool) -> NvCreateChatCompletionRequest {
        let messages = vec![ChatCompletionRequestMessage::User(
            ChatCompletionRequestUserMessage {
                content: ChatCompletionRequestUserMessageContent::Text(
                    "how tall is everest?".to_string(),
                ),
                name: None,
            },
        )];

        let nvext = speculative_prefill.then(|| NvExt {
            agent_hints: Some(AgentHints {
                speculative_prefill: Some(true),
                ..Default::default()
            }),
            ..Default::default()
        });

        NvCreateChatCompletionRequest {
            inner: CreateChatCompletionRequest {
                model: "lifecycle-fixture".to_string(),
                tools: Some(serde_json::from_value(serde_json::json!([
                    {"type": "function", "function": {"name": "lookup_height",
                        "parameters": {"type": "object", "properties": {}}}}
                ])).unwrap()),
                messages,
                stream: Some(true),
                ..Default::default()
            },
            common: Default::default(),
            nvext,
            chat_template_args: None,
            thinking: None,
            thinking_token_budget: None,
            media_io_kwargs: None,
            return_tokens_as_token_ids: None,
            unsupported_fields: Default::default(),
        }
    }

    fn chunk(text: &str, finish: bool) -> Annotated<NvCreateChatCompletionStreamResponse> {
        #[allow(deprecated)]
        let choice = ChatChoiceStream {
            index: 0,
            delta: ChatCompletionStreamResponseDelta {
                role: None,
                content: Some(ChatCompletionMessageContent::Text(text.to_string())),
                tool_calls: finish.then(|| serde_json::from_value(serde_json::json!([
                    {"index": 0, "id": "call-height", "type": "function",
                        "function": {"name": "lookup_height", "arguments": "{}"}}
                ])).unwrap()),
                function_call: None,
                refusal: None,
                reasoning_content: None,
            },
            finish_reason: finish.then_some(FinishReason::ToolCalls),
            logprobs: None,
        };

        Annotated::from_data(NvCreateChatCompletionStreamResponse {
            inner: CreateChatCompletionStreamResponse {
                id: "chatcmpl-test".to_string(),
                object: "chat.completion.chunk".to_string(),
                created: 0,
                model: "lifecycle-fixture".to_string(),
                system_fingerprint: None,
                service_tier: None,
                choices: vec![choice],
                usage: None,
            },
            nvext: None,
            llm_metrics: None,
        })
    }

    fn upstream()
    -> Pin<Box<dyn Stream<Item = Annotated<NvCreateChatCompletionStreamResponse>> + Send>> {
        Box::pin(futures::stream::iter(vec![
            chunk("everest is ", false),
            chunk("8849 m tall.", true),
        ]))
    }

    fn slow_upstream(
        turn: Duration,
    ) -> Pin<Box<dyn Stream<Item = Annotated<NvCreateChatCompletionStreamResponse>> + Send>> {
        Box::pin(
            futures::stream::once(async { chunk("everest is ", false) }).chain(
                futures::stream::once(async move {
                    tokio::time::sleep(turn).await;
                    chunk("8849 m tall.", true)
                }),
            ),
        )
    }

    fn text_of(items: &[Annotated<NvCreateChatCompletionStreamResponse>]) -> String {
        items
            .iter()
            .filter_map(|item| item.data.as_ref())
            .flat_map(|resp| resp.inner.choices.iter())
            .filter_map(|choice| match choice.delta.content {
                Some(ChatCompletionMessageContent::Text(ref text)) => Some(text.as_str()),
                _ => None,
            })
            .collect()
    }

    async fn settle() {
        for _ in 0..16 {
            tokio::task::yield_now().await;
        }
    }

    fn test_tasks(parent: Option<&CancellationToken>) -> PrefillTasks {
        let mut tasks = PrefillTasks::new(parent);
        tasks.preprocessing_admission = Arc::new(Semaphore::new(1));
        tasks
    }

    async fn wait_for_tracked_tasks(tracker: &TaskTracker, expected: usize) {
        // Blocking-pool completion uses wall time, not the paused Tokio clock.
        let started = std::time::Instant::now();
        while tracker.len() != expected {
            assert!(
                started.elapsed() < Duration::from_secs(5),
                "expected {expected} tracked tasks, found {}",
                tracker.len()
            );
            tokio::task::yield_now().await;
        }
    }

    #[test]
    fn replacement_owners_share_process_preprocessing_admission() {
        let first = PrefillTasks::new(None);
        let second = PrefillTasks::new(None);
        assert!(Arc::ptr_eq(
            &first.preprocessing_admission,
            &second.preprocessing_admission
        ));
        let admission = first.preprocessing_admission.clone();
        drop(first);
        drop(second);
        let replacement = PrefillTasks::new(None);
        assert!(Arc::ptr_eq(
            &admission,
            &replacement.preprocessing_admission
        ));
    }

    #[tokio::test(start_paused = true)]
    async fn blocked_preprocessing_retains_tracking_and_admission_after_warmup_ends() {
        use futures::FutureExt;

        struct GatedFormatter {
            inner: Arc<dyn OAIPromptFormatter>,
            entered: Arc<tokio::sync::Notify>,
            calls: Arc<AtomicUsize>,
            release: Mutex<std::sync::mpsc::Receiver<()>>,
        }

        impl OAIPromptFormatter for GatedFormatter {
            fn supports_add_generation_prompt(&self) -> bool {
                self.inner.supports_add_generation_prompt()
            }

            fn render_speculative_tool_prefix(&self, request: &dyn OAIChatLikeRequest) -> Result<Option<String>> {
                self.render(request).map(Some)
            }

            fn render(&self, request: &dyn OAIChatLikeRequest) -> Result<String> {
                self.calls.fetch_add(1, Ordering::SeqCst);
                self.entered.notify_one();
                self.release.lock().recv_timeout(Duration::from_secs(10))?;
                self.inner.render(request)
            }
        }

        #[derive(Clone, Copy)]
        enum Termination {
            OwnerDrop,
            ParentCancellation,
            Timeout,
        }

        for termination in [
            Termination::OwnerDrop,
            Termination::ParentCancellation,
            Termination::Timeout,
        ] {
            let (normal_formatter, tokenizer) = sample_model_parts();
            let (release_tx, release_rx) = std::sync::mpsc::channel();
            let entered = Arc::new(tokio::sync::Notify::new());
            let calls = Arc::new(AtomicUsize::new(0));
            let formatter: Arc<dyn OAIPromptFormatter> = Arc::new(GatedFormatter {
                inner: normal_formatter.clone(),
                entered: entered.clone(),
                calls: calls.clone(),
                release: Mutex::new(release_rx),
            });
            let retained_formatter = Arc::downgrade(&formatter);
            let parent = CancellationToken::new();
            let tasks = test_tasks(Some(&parent));
            let admission = tasks.preprocessing_admission.clone();
            let tracker = tasks.tracker.clone();
            let backend = Arc::new(StallingBackend::default());
            let engine: Arc<BackendEngine> = backend.clone();
            let request = chat_request(true);
            let wrapped = maybe_wrap_stream(
                upstream(),
                &request,
                "req-blocked-preprocessing",
                &engine,
                &formatter,
                &tokenizer,
                &tasks,
                true,
            );
            assert_eq!(wrapped.collect::<Vec<_>>().await.len(), 2);
            wait_for_dispatch(&entered).await;
            assert_eq!(
                tracker.len(),
                2,
                "track the async task and blocking closure"
            );
            assert_eq!(admission.available_permits(), 0);

            match termination {
                Termination::OwnerDrop => drop(tasks),
                Termination::ParentCancellation => {
                    parent.cancel();
                    wait_for_tracked_tasks(&tracker, 1).await;
                    drop(tasks);
                }
                Termination::Timeout => {
                    tokio::time::advance(PREFILL_TASK_TIMEOUT).await;
                    wait_for_tracked_tasks(&tracker, 1).await;
                    drop(tasks);
                }
            }
            wait_for_tracked_tasks(&tracker, 1).await;
            assert!(tracker.is_closed());
            assert!(tracker.wait().now_or_never().is_none());
            assert_eq!(admission.available_permits(), 0);
            assert_eq!(backend.generate_calls(), 0);

            // Replacement owners cannot bypass the permit held by the old closure.
            // Each saturated warmup must finish without queueing blocking work.
            for _ in 0..3 {
                let mut replacement = test_tasks(None);
                replacement.preprocessing_admission = admission.clone();
                let wrapped = maybe_wrap_stream(
                    upstream(),
                    &request,
                    "req-saturated-preprocessing",
                    &engine,
                    &formatter,
                    &tokenizer,
                    &replacement,
                    true,
                );
                assert_eq!(wrapped.collect::<Vec<_>>().await.len(), 2);
                replacement.tracker.close();
                wait_for_tracked_tasks(&replacement.tracker, 0).await;
                assert!(replacement.tracker.wait().now_or_never().is_some());
                assert_eq!(calls.load(Ordering::SeqCst), 1);
                assert_eq!(backend.generate_calls(), 0);
            }

            drop(formatter);
            assert_eq!(retained_formatter.strong_count(), 1);
            release_tx.send(()).unwrap();
            wait_for_tracked_tasks(&tracker, 0).await;
            assert!(tracker.wait().now_or_never().is_some());
            assert_eq!(retained_formatter.strong_count(), 0);
            assert_eq!(admission.available_permits(), 1);
            assert_eq!(
                backend.generate_calls(),
                0,
                "cancelled work must not dispatch"
            );

            // Once the old closure returns, a new owner can use the recovered slot.
            let mut replacement = test_tasks(None);
            replacement.preprocessing_admission = admission.clone();
            let replacement_tracker = replacement.tracker.clone();
            let backend = Arc::new(WindDownBackend::default());
            let engine: Arc<BackendEngine> = backend.clone();
            let wrapped = maybe_wrap_stream(
                upstream(),
                &request,
                "req-recovered-preprocessing",
                &engine,
                &normal_formatter,
                &tokenizer,
                &replacement,
                true,
            );
            assert_eq!(wrapped.collect::<Vec<_>>().await.len(), 2);
            wait_for_dispatch(&backend.dispatched).await;
            assert_eq!(backend.generate_calls.load(Ordering::SeqCst), 1);
            drop(replacement);
            wait_for_tracked_tasks(&replacement_tracker, 0).await;
            assert!(backend.cleanup_ran());
            assert_eq!(admission.available_permits(), 1);
        }
    }

    async fn wait_for_dispatch(dispatched: &tokio::sync::Notify) {
        // Keep paused time from advancing while the blocking pool preprocesses.
        // Wait for an actual dispatch rather than assuming a fixed yield count.
        let started = std::time::Instant::now();
        loop {
            tokio::select! {
                biased;
                () = dispatched.notified() => return,
                () = tokio::task::yield_now() => {}
            }
            assert!(
                started.elapsed() < Duration::from_secs(10),
                "preprocessing did not dispatch the warmup"
            );
        }
    }

    #[tokio::test]
    async fn cancellation_during_blocked_preprocessing_prevents_dispatch() {
        struct BlockingFormatter {
            inner: Arc<dyn OAIPromptFormatter>,
            entered: Mutex<Option<tokio::sync::oneshot::Sender<()>>>,
            release: Mutex<std::sync::mpsc::Receiver<()>>,
        }

        impl OAIPromptFormatter for BlockingFormatter {
            fn supports_add_generation_prompt(&self) -> bool {
                self.inner.supports_add_generation_prompt()
            }

            fn render_speculative_tool_prefix(&self, request: &dyn OAIChatLikeRequest) -> Result<Option<String>> {
                self.render(request).map(Some)
            }

            fn render(&self, request: &dyn OAIChatLikeRequest) -> Result<String> {
                self.entered.lock().take().unwrap().send(()).unwrap();
                self.release.lock().recv_timeout(Duration::from_secs(10))?;
                self.inner.render(request)
            }
        }

        let (formatter, tokenizer) = sample_model_parts();
        let (entered_tx, entered_rx) = tokio::sync::oneshot::channel();
        let (release_tx, release_rx) = std::sync::mpsc::channel();
        let formatter = Arc::new(BlockingFormatter {
            inner: formatter,
            entered: Mutex::new(Some(entered_tx)),
            release: Mutex::new(release_rx),
        });
        let cancel = CancellationToken::new();
        let cancelling_task = {
            let cancel = cancel.clone();
            tokio::spawn(async move {
                entered_rx.await.unwrap();
                cancel.cancel();
                release_tx.send(()).unwrap();
            })
        };
        let backend = Arc::new(StallingBackend::default());
        let context_slot = PrefillContextSlot::new(None);
        let tasks = test_tasks(None);
        // Call the warmup directly so the outer select cannot mask a missing
        // post-preprocessing cancellation check. The current-thread runtime also
        // requires rendering to yield the executor for the cancelling task.
        tokio::time::timeout(
            Duration::from_secs(5),
            prefill_task(
                backend.clone(),
                tasks.preprocessing(formatter, tokenizer),
                chat_request(true),
                completed_assistant(),
                &context_slot,
                &cancel,
            ),
        )
        .await
        .expect("cancelled rendering must not dispatch a stalled warmup")
        .unwrap();
        cancelling_task.await.unwrap();
        assert!(cancel.is_cancelled());
        assert!(context_slot.lock().is_none());
        assert_eq!(backend.generate_calls(), 0);
    }

    #[tokio::test]
    async fn cancellation_during_tokenization_prevents_dispatch() {
        use crate::tokenizers::traits::{Decoder, Encoder};
        use crate::tokenizers::{DecodeResult, EncodeSegment, Encoding};

        struct BlockingTokenizer {
            inner: Arc<dyn Tokenizer>,
            entered: Mutex<Option<tokio::sync::oneshot::Sender<()>>>,
            release: Mutex<std::sync::mpsc::Receiver<()>>,
        }

        impl Encoder for BlockingTokenizer {
            fn encode(&self, input: &str) -> Result<Encoding> {
                if input != "<|im_end|>" {
                    if let Some(entered) = self.entered.lock().take() {
                        entered.send(()).unwrap();
                        self.release.lock().recv_timeout(Duration::from_secs(10))?;
                    }
                }
                self.inner.encode(input)
            }

            fn encode_batch(&self, inputs: &[&str]) -> Result<Vec<Encoding>> {
                self.inner.encode_batch(inputs)
            }

            fn encode_segments(&self, segments: &[EncodeSegment<'_>]) -> Result<Encoding> {
                self.inner.encode_segments(segments)
            }
        }

        impl Decoder for BlockingTokenizer {
            fn decode(&self, token_ids: &[u32], skip_special_tokens: bool) -> Result<DecodeResult> {
                self.inner.decode(token_ids, skip_special_tokens)
            }
        }

        impl Tokenizer for BlockingTokenizer {
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
        }

        let (formatter, tokenizer) = sample_model_parts();
        let (entered_tx, entered_rx) = tokio::sync::oneshot::channel();
        let (release_tx, release_rx) = std::sync::mpsc::channel();
        let tokenizer = Arc::new(BlockingTokenizer {
            inner: tokenizer,
            entered: Mutex::new(Some(entered_tx)),
            release: Mutex::new(release_rx),
        });
        let cancel = CancellationToken::new();
        let cancelling_task = {
            let cancel = cancel.clone();
            tokio::spawn(async move {
                entered_rx.await.unwrap();
                cancel.cancel();
                release_tx.send(()).unwrap();
            })
        };
        let backend = Arc::new(StallingBackend::default());
        let context_slot = PrefillContextSlot::new(None);
        let tasks = test_tasks(None);
        // Cancellation occurs after the formatter check. Call directly so only
        // the final pre-dispatch check can prevent the downstream request.
        tokio::time::timeout(
            Duration::from_secs(5),
            prefill_task(
                backend.clone(),
                tasks.preprocessing(formatter, tokenizer),
                chat_request(true),
                completed_assistant(),
                &context_slot,
                &cancel,
            ),
        )
        .await
        .expect("cancelled tokenization must not dispatch a stalled warmup")
        .unwrap();
        tokio::time::timeout(Duration::from_secs(5), cancelling_task)
            .await
            .expect("the tokenizer must signal entry and receive cancellation")
            .unwrap();
        assert!(cancel.is_cancelled());
        assert!(context_slot.lock().is_none());
        assert_eq!(backend.generate_calls(), 0);
    }

    #[tokio::test(start_paused = true)]
    async fn dropping_owner_stops_and_drains_a_permanently_pending_warmup() {
        let (formatter, tokenizer) = sample_model_parts();
        let backend = Arc::new(StallingBackend::default());
        let engine: Arc<BackendEngine> = backend.clone();
        let tasks = test_tasks(None);
        let tracker = tasks.tracker.clone();
        let request = chat_request(true);
        let wrapped = maybe_wrap_stream(
            upstream(),
            &request,
            "req-owner-drop",
            &engine,
            &formatter,
            &tokenizer,
            &tasks,
            true,
        );
        drop(engine);
        let items: Vec<_> = wrapped.collect().await;
        assert_eq!(items.len(), 2);
        wait_for_dispatch(&backend.dispatched).await;
        assert_eq!(tracker.len(), 1);

        let stopped = backend.context.lock().as_ref().unwrap().clone();
        let started = Instant::now();
        drop(tasks);
        stopped.stopped().await;
        assert!(
            !backend.stream_dropped(),
            "cleanup must get its grace period"
        );
        assert!(!tracker.is_empty(), "the draining task must remain tracked");
        tracker.wait().await;

        assert_eq!(started.elapsed(), PREFILL_WIND_DOWN_GRACE);
        assert!(backend.stream_dropped());
        assert_eq!(Arc::strong_count(&backend), 1);
    }

    #[tokio::test(start_paused = true)]
    async fn dropping_owner_cancels_tasks_waiting_for_client_completion() {
        let (formatter, tokenizer) = sample_model_parts();
        let backend = Arc::new(StallingBackend::default());
        let engine: Arc<BackendEngine> = backend.clone();
        let tasks = test_tasks(None);
        let tracker = tasks.tracker.clone();
        let request = chat_request(true);
        let wrapped = maybe_wrap_stream(
            upstream(),
            &request,
            "req-owner-before-dispatch",
            &engine,
            &formatter,
            &tokenizer,
            &tasks,
            true,
        );
        drop(engine);
        assert_eq!(tracker.len(), 1);
        drop(tasks);
        tracker.wait().await;

        let items: Vec<_> = wrapped.collect().await;
        assert_eq!(items.len(), 2);
        assert_eq!(backend.generate_calls(), 0);
        assert_eq!(Arc::strong_count(&backend), 1);
    }

    #[tokio::test(start_paused = true)]
    async fn stalled_warmup_is_released_when_the_lifetime_bound_elapses() {
        let (formatter, tokenizer) = sample_model_parts();
        let backend = Arc::new(StallingBackend::default());
        let engine: Arc<BackendEngine> = backend.clone();

        let request = chat_request(true);
        let tasks = test_tasks(None);
        let wrapped = maybe_wrap_stream(
            upstream(),
            &request,
            "req-bound",
            &engine,
            &formatter,
            &tokenizer,
            &tasks,
            true,
        );
        drop(engine);

        let items: Vec<_> = wrapped.collect().await;
        assert_eq!(
            items.len(),
            2,
            "client stream must be passed through intact"
        );

        wait_for_dispatch(&backend.dispatched).await;
        assert_eq!(backend.generate_calls(), 1, "warmup should have dispatched");
        tokio::time::sleep(PREFILL_TASK_TIMEOUT / 2).await;
        settle().await;
        assert!(
            !backend.stream_dropped(),
            "warmup abandoned before its bound elapsed"
        );
        assert_eq!(Arc::strong_count(&backend), 2);

        tokio::time::sleep(PREFILL_TASK_TIMEOUT).await;
        settle().await;

        assert!(
            backend.was_asked_to_stop(),
            "downstream context should be told to stop before the stream is dropped"
        );
        assert!(
            backend.stream_dropped(),
            "stalled downstream stream should be released once the bound elapses"
        );
        assert_eq!(
            Arc::strong_count(&backend),
            1,
            "tracked task should have released the engine it captured"
        );
    }

    #[tokio::test(start_paused = true)]
    async fn a_turn_longer_than_the_bound_still_gets_its_warmup() {
        let (formatter, tokenizer) = sample_model_parts();
        let backend = Arc::new(StallingBackend::default());
        let engine: Arc<BackendEngine> = backend.clone();

        let request = chat_request(true);
        let tasks = test_tasks(None);
        let wrapped = maybe_wrap_stream(
            slow_upstream(PREFILL_TASK_TIMEOUT * 2),
            &request,
            "req-long-turn",
            &engine,
            &formatter,
            &tokenizer,
            &tasks,
            true,
        );
        drop(engine);

        let started = Instant::now();
        let items: Vec<_> = wrapped.collect().await;
        assert_eq!(items.len(), 2);
        assert!(
            started.elapsed() > PREFILL_TASK_TIMEOUT,
            "the turn must outlast the bound for this test to mean anything"
        );

        let dispatched = Instant::now();
        wait_for_dispatch(&backend.dispatched).await;
        assert_eq!(
            backend.generate_calls(),
            1,
            "a turn longer than the bound must still dispatch its warmup"
        );

        tokio::time::sleep(PREFILL_TASK_TIMEOUT / 2).await;
        settle().await;
        assert!(
            !backend.stream_dropped(),
            "warmup abandoned before its own bound elapsed"
        );

        tokio::time::sleep(PREFILL_TASK_TIMEOUT).await;
        settle().await;
        assert!(backend.was_asked_to_stop());
        assert!(
            backend.stream_dropped(),
            "warmup should be released once its own bound elapses"
        );
        assert_eq!(Arc::strong_count(&backend), 1);
        assert!(dispatched.elapsed() < PREFILL_TASK_TIMEOUT * 2);
    }

    #[tokio::test(start_paused = true)]
    async fn stalled_warmup_is_released_when_the_runtime_token_is_cancelled() {
        let (formatter, tokenizer) = sample_model_parts();
        let backend = Arc::new(StallingBackend::default());
        let engine: Arc<BackendEngine> = backend.clone();
        let cancel = CancellationToken::new();

        let request = chat_request(true);
        let tasks = test_tasks(Some(&cancel));
        let wrapped = maybe_wrap_stream(
            upstream(),
            &request,
            "req-cancel",
            &engine,
            &formatter,
            &tokenizer,
            &tasks,
            true,
        );
        drop(engine);

        let started = Instant::now();
        let items: Vec<_> = wrapped.collect().await;
        assert_eq!(items.len(), 2);

        wait_for_dispatch(&backend.dispatched).await;
        assert_eq!(backend.generate_calls(), 1, "warmup should have dispatched");
        assert!(!backend.stream_dropped());

        cancel.cancel();
        settle().await;

        assert!(
            backend.was_asked_to_stop(),
            "cancellation should reach the downstream context"
        );

        tokio::time::sleep(PREFILL_WIND_DOWN_GRACE).await;
        settle().await;

        assert!(
            backend.stream_dropped(),
            "a downstream that ignores the stop should still be released once the grace elapses"
        );
        assert_eq!(Arc::strong_count(&backend), 1);
        assert!(started.elapsed() < PREFILL_TASK_TIMEOUT);
    }

    #[tokio::test(start_paused = true)]
    async fn a_downstream_that_honours_the_stop_completes_its_own_cleanup() {
        let (formatter, tokenizer) = sample_model_parts();
        let backend = Arc::new(WindDownBackend::default());
        let engine: Arc<BackendEngine> = backend.clone();
        let cancel = CancellationToken::new();

        let request = chat_request(true);
        let tasks = test_tasks(Some(&cancel));
        let wrapped = maybe_wrap_stream(
            upstream(),
            &request,
            "req-wind-down",
            &engine,
            &formatter,
            &tokenizer,
            &tasks,
            true,
        );
        drop(engine);

        let items: Vec<_> = wrapped.collect().await;
        assert_eq!(items.len(), 2);

        wait_for_dispatch(&backend.dispatched).await;
        assert!(!backend.cleanup_ran(), "nothing should wind down yet");

        let cancelled_at = Instant::now();
        cancel.cancel();
        settle().await;

        assert!(backend.was_asked_to_stop());
        assert!(
            backend.cleanup_ran(),
            "a downstream watching its context must get to run its cleanup; \
             signalling and dropping in the same poll would skip it"
        );
        assert!(
            backend.stream_dropped(),
            "the stream should be released once it has wound down"
        );
        assert!(
            cancelled_at.elapsed() < PREFILL_WIND_DOWN_GRACE,
            "wind-down should complete inside the grace window"
        );
        assert_eq!(Arc::strong_count(&backend), 1);
    }

    #[tokio::test(start_paused = true)]
    async fn an_already_cancelled_token_dispatches_nothing() {
        let (formatter, tokenizer) = sample_model_parts();
        let backend = Arc::new(StallingBackend::default());
        let engine: Arc<BackendEngine> = backend.clone();

        let cancel = CancellationToken::new();
        cancel.cancel();

        let request = chat_request(true);
        let tasks = test_tasks(Some(&cancel));
        let wrapped = maybe_wrap_stream(
            upstream(),
            &request,
            "req-already-cancelled",
            &engine,
            &formatter,
            &tokenizer,
            &tasks,
            true,
        );
        drop(engine);

        let items: Vec<_> = wrapped.collect().await;
        assert_eq!(
            items.len(),
            2,
            "the client stream is passed through regardless"
        );

        settle().await;
        tokio::time::sleep(PREFILL_TASK_TIMEOUT).await;
        settle().await;

        assert_eq!(
            backend.generate_calls(),
            0,
            "no warmup may be dispatched once the runtime is already shutting down"
        );
        assert_eq!(
            Arc::strong_count(&backend),
            1,
            "the task should have released the engine without dispatching"
        );
    }

    #[tokio::test(start_paused = true)]
    async fn a_warmup_still_waiting_for_a_worker_is_reachable_by_the_stop() {
        let (formatter, tokenizer) = sample_model_parts();
        let backend = Arc::new(StalledInGenerateBackend::default());
        let engine: Arc<BackendEngine> = backend.clone();

        let cancel = CancellationToken::new();
        let request = chat_request(true);
        let tasks = test_tasks(Some(&cancel));
        let wrapped = maybe_wrap_stream(
            upstream(),
            &request,
            "req-stalled-in-generate",
            &engine,
            &formatter,
            &tokenizer,
            &tasks,
            true,
        );
        drop(engine);

        let items: Vec<_> = wrapped.collect().await;
        assert_eq!(items.len(), 2);

        wait_for_dispatch(&backend.dispatched).await;
        assert_eq!(
            backend.generate_calls(),
            1,
            "the warmup should be in flight, still waiting for a worker"
        );

        cancel.cancel();
        settle().await;

        assert!(
            backend.was_asked_to_stop(),
            "a warmup stuck in generate must still be reachable by the stop"
        );

        settle().await;
        assert_eq!(
            Arc::strong_count(&backend),
            1,
            "the task should have released the engine once generate returned"
        );
    }

    #[tokio::test(start_paused = true)]
    async fn without_the_hint_the_stream_is_untouched_and_nothing_is_dispatched() {
        let (formatter, tokenizer) = sample_model_parts();
        let backend = Arc::new(StallingBackend::default());
        let engine: Arc<BackendEngine> = backend.clone();

        let request = chat_request(false);
        let tasks = test_tasks(None);
        let wrapped = maybe_wrap_stream(
            upstream(),
            &request,
            "req-disabled",
            &engine,
            &formatter,
            &tokenizer,
            &tasks,
            true,
        );
        drop(engine);

        let items: Vec<_> = wrapped.collect().await;
        assert_eq!(items.len(), 2);
        assert_eq!(text_of(&items), "everest is 8849 m tall.");

        settle().await;
        tokio::time::sleep(PREFILL_TASK_TIMEOUT * 2).await;
        settle().await;

        assert_eq!(
            backend.generate_calls(),
            0,
            "the disabled path must never dispatch a warmup"
        );
        assert_eq!(
            Arc::strong_count(&backend),
            1,
            "the disabled path must not capture the engine at all"
        );
    }
}
