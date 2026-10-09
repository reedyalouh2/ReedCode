import base64
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import analyze as a


def chunks(events, split=False):
    data=b''.join(('event: '+e['type']+'\r\ndata: '+json.dumps(e,ensure_ascii=False)+'\r\n\r\n').encode() for e in events)
    pieces=[data[:7],data[7:41],data[41:]] if split else [data]
    return [{'at_ns':1000000000+i*1000000000,'data_base64':base64.b64encode(piece).decode()} for i,piece in enumerate(pieces)]


def text_events(text='4',stop='end_turn',count=2):
    return [
        {'type':'message_start','message':{'usage':{'input_tokens':999,'output_tokens':0}}},
        {'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}},
        {'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':text}},
        {'type':'content_block_stop','index':0},
        {'type':'message_delta','delta':{'stop_reason':stop},'usage':{'input_tokens':3,'output_tokens':count}},
        {'type':'message_stop'}]


class AnalyzerTests(unittest.TestCase):
    def test_chunk_split_utf8_crlf_and_final_usage(self):
        events,errors=a.sse_events(chunks(text_events('é'),True))
        result=a.parse_message(events,{})
        self.assertEqual(errors,[])
        self.assertEqual(result['final_text'],'é')
        self.assertEqual(result['final_usage']['input_tokens'],3)
        self.assertEqual(result['first_final_text_ns'],3000000000)

    def test_truncated_sse_is_not_silently_accepted(self):
        events,errors=a.sse_events([{'at_ns':1,'data_base64':base64.b64encode(b'data: {"type":').decode()}])
        self.assertEqual(events,[])
        self.assertIn('truncated_sse_event',errors)

    def test_multispan_does_not_count_leading_content_or_eos(self):
        result=a.reasoning_spans(a.ASSISTANT,[42,a.THINK,1,2,a.END_THINK,3,a.THINK,4,a.END_THINK,a.EOS])
        self.assertEqual(result['reasoning_tokens'],3)
        self.assertEqual(result['reasoning_delimiter_tokens'],4)
        self.assertEqual(result['eos_tokens'],1)

    def test_unclosed_reasoning_and_disabled_prompt_state(self):
        result=a.reasoning_spans(a.DISABLED,[a.THINK,1,2])
        self.assertEqual(result['reasoning_tokens'],2)
        self.assertTrue(result['open_reasoning_at_end'])
        self.assertEqual(a.reasoning_spans(a.ASSISTANT,[1,2])['reasoning_tokens'],0)

    def test_orphan_close_and_tool_literal_markers_are_unknown(self):
        self.assertIsNone(a.reasoning_spans(a.ASSISTANT,[1,a.END_THINK])['reasoning_tokens'])
        self.assertIsNone(a.reasoning_spans(a.ASSISTANT,[a.TOOL,a.THINK,1,a.END_TOOL])['reasoning_tokens'])
        self.assertIsNone(a.reasoning_spans(a.ASSISTANT,[a.THINK,1,a.TOOL,2])['reasoning_tokens'])

    def test_tool_requires_closed_valid_json_and_known_name(self):
        body={'tools':[{'name':'read','input_schema':{'type':'object','properties':{'path':{'type':'string'}},'required':['path']}}]}
        events=[{'type':'message_start','message':{}},
                {'type':'content_block_start','index':0,'content_block':{'type':'tool_use','id':'x','name':'read','input':{}}},
                {'type':'content_block_delta','index':0,'delta':{'type':'input_json_delta','partial_json':'{"path":'}},
                {'type':'content_block_stop','index':0},
                {'type':'message_delta','delta':{'stop_reason':'tool_use'},'usage':{'output_tokens':9}},
                {'type':'message_stop'}]
        parsed,_=a.sse_events(chunks(events))
        result=a.parse_message(parsed,body)
        self.assertFalse(result['tool_calls'][0]['structurally_valid'])
        self.assertIsNone(result['first_valid_complete_tool_ns'])
        events[2]['delta']['partial_json']='{"path":"x"}'
        parsed,_=a.sse_events(chunks(events))
        result=a.parse_message(parsed,body)
        self.assertTrue(result['tool_calls'][0]['structurally_valid'])
        if a.jsonschema is None: self.assertIsNone(result['first_valid_complete_tool_ns'])
        else:
            self.assertTrue(result['tool_calls'][0]['schema_valid'])
            self.assertEqual(result['first_valid_complete_tool_ns'],1000000000)
            events[2]['delta']['partial_json']='{"path":3}'
            parsed,_=a.sse_events(chunks(events))
            invalid=a.parse_message(parsed,body)
            self.assertFalse(invalid['tool_calls'][0]['schema_valid'])
            self.assertIsNone(invalid['first_valid_complete_tool_ns'])
        events[4]['delta']['stop_reason']='max_tokens'
        parsed,_=a.sse_events(chunks(events))
        self.assertIsNone(a.parse_message(parsed,body)['first_valid_complete_tool_ns'])

    def fixture_row(self,directory,stop='end_turn',transport_status='eof',partial=False):
        body={'temperature':0,'max_tokens':2048,'messages':[]}
        raw=json.dumps(body).encode()
        entry={'request_id':'r','pair_id':'p','arm':'stock','repeat':1,'order':1,'fixture_id':'f','category':'coding_correctness',
               'body_sha256':hashlib.sha256(raw).hexdigest(),'headers':{'x-tenant-id':'salt'}}
        folder=directory/'r'; folder.mkdir()
        (folder/'request.body').write_bytes(raw)
        (folder/'transport.json').write_text(json.dumps({'sent_ns':0,'done_ns':2000000000,'transport_status':transport_status,'http_status':200}))
        (folder/'response-chunks.jsonl').write_text('\n'.join(json.dumps(c) for c in chunks(text_events('4',stop,2)))+'\n')
        engine={'kind':'engine_result','cache_salt':'dynamo-cache-salt:salt','request_id':'uuid','prompt_ids':a.ASSISTANT,
                'output_ids':[19,a.EOS],'output_ids_complete':not partial,'prompt_ids_match_final':True,'cached_tokens':0,
                'engine_stats':{'num_generation_tokens':2},'engine_finish_reason':'length' if stop=='max_tokens' else 'stop',
                'sampling':{'temperature':0,'max_tokens':2048,'n':1},'output_kind':'DELTA'}
        (folder/'engine-records.json').write_text(json.dumps([{'kind':'engine_enter','cache_salt':engine['cache_salt'],'request_id':'uuid'},engine]))
        fixture={'category':'coding_correctness','acceptance':{'expected_stdout':'4'}}
        return a.analyze_row(entry,fixture,directory,directory,None,{'f':{'stock':a.ASSISTANT}})

    def test_limit_hit_kept_even_if_partial_answer_matches(self):
        with TemporaryDirectory() as d: row=self.fixture_row(Path(d),stop='max_tokens')
        self.assertEqual(row['status'],'limit_hit')
        self.assertTrue(row['answer_exact_match'])
        self.assertFalse(row['answer_accepted'])
        self.assertTrue(row['cost_eligible'])
        self.assertEqual(row['output_tokens'],2)

    def test_timeout_is_censored_and_partial_count_is_not_total(self):
        with TemporaryDirectory() as d: row=self.fixture_row(Path(d),transport_status='timeout',partial=True)
        self.assertEqual(row['status'],'timeout')
        self.assertIsNone(row['latency_s'])
        self.assertEqual(row['observed_elapsed_s'],2)
        self.assertEqual(row['output_ids_count'],2)
        self.assertIsNone(row['output_tokens'])
        self.assertFalse(row['answer_accepted'])

    def test_not_started_row_is_retained(self):
        entry={'request_id':'r','pair_id':'p','arm':'stock','fixture_id':'f'}
        with TemporaryDirectory() as d: row=a.analyze_row(entry,{},Path(d),Path(d),{'not_started':'time_cutoff'})
        self.assertEqual(row['not_started_reason'],'time_cutoff')
        self.assertEqual(row['status'],'not_started')

    def test_terminal_without_final_reason_or_with_extra_events_is_invalid(self):
        events=text_events()
        events[-2]['delta']={}
        parsed,_=a.sse_events(chunks(events))
        self.assertIn('missing_or_unknown_final_stop_reason',a.parse_message(parsed,{})['errors'])
        events=text_events()+[{'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'late'}}]
        parsed,_=a.sse_events(chunks(events))
        self.assertIn('event_after_message_stop',a.parse_message(parsed,{})['errors'])

    def test_prompt_mismatch_blocks_cost_comparison(self):
        with TemporaryDirectory() as d:
            directory=Path(d); row=self.fixture_row(directory)
            row=a.analyze_row(row,{'category':'coding_correctness','acceptance':{'expected_stdout':'4'}},directory,directory,None,{'f':{'stock':[9]}})
        self.assertFalse(row['prompt_matches_cpu'])
        self.assertFalse(row['cost_eligible'])

    def test_failing_pair_remains_and_no_cost_difference_is_fabricated(self):
        with TemporaryDirectory() as d: left=self.fixture_row(Path(d))
        right={**left,'arm':'patched','status':'timeout','cost_eligible':False,'latency_s':None,'answer_accepted':False}
        result=a.summarize([left,right])
        pair=result['fixtures'][0]['pairs'][0]
        self.assertEqual(pair['patched_status'],'timeout')
        self.assertIsNone(pair['output_tokens_patched_minus_stock'])
        self.assertEqual(pair['answer_accepted_patched_minus_stock'],-1)


if __name__=='__main__': unittest.main()
