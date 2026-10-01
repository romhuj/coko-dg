package org.coyote.mobile;

import android.content.Context;
import android.os.Bundle;
import android.test.InstrumentationTestCase;
import android.test.InstrumentationTestRunner;
import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import org.json.JSONObject;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;

/**
 * Opt-in attachment to an already running app. Never starts Python, an Activity,
 * or RuntimeService, and never clears app data. Excluded from normal smoke runs
 * unless allow_live_model_probe=true is explicitly supplied.
 *
 * Run only this method with am instrument --no-restart and the opt-in argument.
 * The output is metadata only; no prompt, response text, history text, or key is
 * written to storage or instrumentation status. Probe responses never enter the
 * device action executor or live conversation history.
 */
public final class LiveModelDiagnosticTest extends InstrumentationTestCase {
    private Bundle arguments() {
        return getInstrumentation() instanceof InstrumentationTestRunner
            ? ((InstrumentationTestRunner)getInstrumentation()).getArguments() : new Bundle();
    }
    private boolean allowed() {
        return "true".equalsIgnoreCase(arguments().getString("allow_live_model_probe", "false"));
    }
    private File reportFile() throws Exception {
        Context context=getInstrumentation().getTargetContext();
        File folder=context.getExternalFilesDir(null);
        if(folder==null) throw new IllegalStateException("External app report directory unavailable");
        return new File(folder,"live-model-diagnostic.json");
    }
    private void writeJavaStatus(String mode,String status) throws Exception {
        JSONObject report=new JSONObject();
        report.put("mode",mode); report.put("status",status);
        report.put("recorded_at_ms",System.currentTimeMillis());
        report.put("python_started",Python.isStarted());
        try(FileOutputStream stream=new FileOutputStream(reportFile())) {
            stream.write(report.toString(2).getBytes(StandardCharsets.UTF_8));
        }
    }
    private PyObject existingPythonGlobals(String mode) throws Exception {
        if(!Python.isStarted()) {
            writeJavaStatus(mode,"not_running");
            fail("Existing Python runtime is not running; no service was started");
        }
        PyObject scope=Python.getInstance().getModule("builtins").callAttr("dict");
        scope.callAttr("__setitem__","report_path",reportFile().getAbsolutePath());
        return scope;
    }

    /** Safe alternative method selection; this never contacts a model or stops output. */
    public void testPingOnly() throws Exception {
        if(!allowed() || !arguments().getString("class","").contains("#testPingOnly")) return;
        PyObject scope=existingPythonGlobals("ping");
        try {
            Python.getInstance().getModule("builtins").callAttr("exec", """
                import sys, json, time
                from pathlib import Path
                module = sys.modules.get('backend.mobile_runtime')
                runtime = getattr(module, '_runtime', None)
                running = bool(runtime and runtime.app and runtime.loop and runtime.loop.is_running()
                               and not runtime.finished.is_set())
                ping_report = {'mode':'ping', 'status':'running' if running else 'not_running',
                               'recorded_at_ms':round(time.time()*1000), 'python_started':True}
                Path(report_path).write_text(json.dumps(ping_report, indent=2), encoding='utf-8')
                """,scope);
            assertEquals("No existing runtime; no service was started", "running",
                scope.callAttr("__getitem__","ping_report").callAttr("get","status").toString());
        } catch(AssertionError failure) { throw failure; }
        catch(Exception failure) {
            writeJavaStatus("ping","diagnostic_internal_error");
            fail("Ping failed without starting a service ("+failure.getClass().getSimpleName()+")");
        }
    }

    public void testCurrentHistoryProbe() throws Exception {
        if(!allowed()) return;
        PyObject scope=existingPythonGlobals("current_history_probe");
        try {
            Python.getInstance().getModule("builtins").callAttr("exec", DIAGNOSTIC, scope);
            String status=scope.callAttr("__getitem__","diagnostic_status").toString();
            assertEquals("Live diagnostic did not complete; see metadata report", "completed",status);
        } catch(AssertionError failure) { throw failure; }
        catch(Exception failure) {
            // Do not surface exception messages: provider errors can contain
            // request details. Keep an already-written partial report intact.
            if(!reportFile().isFile()) writeJavaStatus("current_history_probe","diagnostic_internal_error");
            fail("Diagnostic interrupted; inspect metadata report ("+failure.getClass().getSimpleName()+")");
        }
    }

    /** Exercises the installed model path twice, without touching live history or actions. */
    public void testProductionModelProbe() throws Exception {
        if(!allowed() || !arguments().getString("class","").contains("#testProductionModelProbe")) return;
        PyObject scope=existingPythonGlobals("production_model_probe");
        scope.callAttr("__setitem__","production_only",true);
        try {
            Python.getInstance().getModule("builtins").callAttr("exec", DIAGNOSTIC, scope);
            assertEquals("Production model probe did not complete; see metadata report", "completed",
                scope.callAttr("__getitem__","diagnostic_status").toString());
            assertEquals("Both production model turns must succeed", 2,
                scope.callAttr("__getitem__","report").callAttr("get","production_success_count",0).toInt());
        } catch(AssertionError failure) { throw failure; }
        catch(Exception failure) {
            if(!reportFile().isFile()) writeJavaStatus("production_model_probe","diagnostic_internal_error");
            fail("Production probe interrupted; inspect metadata report ("+failure.getClass().getSimpleName()+")");
        }
    }

    private static final String DIAGNOSTIC = """
        import asyncio, copy, datetime, json, math, re, sys, time
        from pathlib import Path
        from urllib.parse import urlsplit

        def stamp():
            return datetime.datetime.now(datetime.timezone.utc).isoformat()

        production_only = bool(globals().get('production_only',False))
        report = {'mode':'production_model_probe' if production_only else 'current_history_probe', 'status':'checking_runtime',
                  'started_at':stamp(), 'attempts':[], 'max_tokens':1500, 'request_timeout_s':45}

        def persist():
            Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

        def model_name(value):
            return value if isinstance(value, str) and re.fullmatch(r'deepseek[-A-Za-z0-9._:/]{1,120}',value) else 'unrecognized'

        def text_content(value):
            if isinstance(value,str): return value
            if isinstance(value,list):
                return ''.join(part.get('text','') for part in value if isinstance(part,dict)
                               and part.get('type') in ('text','output_text') and isinstance(part.get('text'),str))
            return ''

        def message_metadata(messages):
            allowed_roles = {'system','developer','user','assistant','tool','function'}
            return [{'role':message.get('role') if message.get('role') in allowed_roles else 'unknown',
                     'content_chars':len(text_content(message.get('content')))} for message in messages]

        def usage_metadata(value):
            allowed_keys = {'prompt_tokens','completion_tokens','total_tokens','prompt_cache_hit_tokens',
                            'prompt_cache_miss_tokens','input_tokens','output_tokens','reasoning_tokens',
                            'cached_tokens','accepted_prediction_tokens','rejected_prediction_tokens',
                            'completion_tokens_details','prompt_tokens_details','input_tokens_details',
                            'output_tokens_details'}
            if not isinstance(value,dict): return {}
            result = {}
            for key,item in value.items():
                if key not in allowed_keys: continue
                if type(item) is int and 0 <= item <= 1000000000: result[key] = item
                elif isinstance(item,dict): result[key] = usage_metadata(item)
            return result

        def parsed_metadata(content):
            clean = content.lstrip('\\ufeff').strip()
            fenced = re.fullmatch(r'```(?:json)?\\s*\\n?(.*?)\\n?```',clean,flags=re.I|re.S)
            if fenced: clean = fenced.group(1).strip()
            try:
                data = json.loads(clean)
                if isinstance(data,dict):
                    line = data.get('line')
                    return {'json_object':True, 'line_present':isinstance(line,str) and bool(line.strip()),
                            'action_count':len(data['actions']) if isinstance(data.get('actions'),list) else None}
            except (ValueError,TypeError): pass
            return {'json_object':False, 'line_present':False, 'action_count':None}

        def error_metadata(value):
            # Never persist exception messages or unknown provider fields.
            if not isinstance(value,dict): return {}
            output = {}
            numeric = {'attempts','attempt','completion_tokens','reasoning_tokens'}
            finishes = {'stop','length','content_filter','tool_calls','function_call','aborted','insufficient_system_resource','unknown',None}
            for field,item in value.items():
                if field in numeric and type(item) is int and 0 <= item <= 10000000:
                    output[field] = item
                elif field in {'json_mode','empty_content'} and type(item) is bool:
                    output[field] = item
                elif field == 'code' and isinstance(item,str):
                    output[field] = item if item in {'truncated','interrupted','empty_content','invalid_response'} else 'unknown'
                elif field == 'finish_reason' and isinstance(item,(str,type(None))):
                    output[field] = item if item in finishes else 'unknown'
                elif field == 'response_attempts' and isinstance(item,list):
                    output[field] = [error_metadata(entry) for entry in item[:4] if isinstance(entry,dict)]
            return output

        module = sys.modules.get('backend.mobile_runtime')
        diagnostic_runtime = getattr(module,'_runtime',None)
        running = bool(diagnostic_runtime and diagnostic_runtime.app and diagnostic_runtime.loop
                       and diagnostic_runtime.loop.is_running() and not diagnostic_runtime.finished.is_set())
        persist()

        async def diagnose(runtime):
            import httpx
            from backend.llm import build_system_prompt
            live = runtime.app.state.runtime
            loop = live.loop
            snapshot_error = None
            try:
                character = copy.deepcopy(live.cfg['character'])
                settings = copy.deepcopy(live.cfg['llm'])
                history = copy.deepcopy(loop.history)
                device_state = copy.deepcopy(loop.build_state())
                keep = int(loop.keep)
                current_llm = live.llm
                key = str(getattr(current_llm,'api_key',settings.get('api_key') or ''))
                model = str(getattr(current_llm,'model',settings.get('model') or ''))
                temperature = float(getattr(current_llm,'temperature',settings.get('temperature',1.0)))
                report.update(history_length=len(history), character_prompt_chars=len(str(character.get('prompt') or '')),
                              configured_model=model_name(model), history_keep=keep,
                              snapshot_estop=bool(device_state.get('estop')), snapshot_autopilot=bool(device_state.get('autopilot')))
            except Exception as failure:
                snapshot_error = type(failure).__name__
            # Latch protection before any diagnostic network request. These are
            # the only live behavior changes; nothing restores either setting.
            loop.set_autopilot(False)
            loop.stop_observe_loop()
            must_send = live.backend.ready() and not live.safety.dry_run
            try:
                stopped = await asyncio.wait_for(loop.estop(),timeout=15)
                report['stop_confirmed'] = not must_send or bool(stopped.get('sent'))
            except Exception as failure:
                report['stop_confirmed'] = False
                report['stop_error_type'] = type(failure).__name__
            report['output_locked'] = bool(live.safety.estop_active)
            report['autopilot_disabled'] = not loop.autopilot
            try: await asyncio.wait_for(live.broadcast(),timeout=5)
            except Exception: report['broadcast_confirmed'] = False
            if not report['output_locked'] or not report['stop_confirmed']:
                report['status']='stop_unconfirmed'; persist(); return
            if snapshot_error:
                report.update(status='snapshot_failed',error_type=snapshot_error); persist(); return
            base = str(settings.get('base_url') or '').strip().rstrip('/')
            try:
                address = urlsplit(base)
                official = address.scheme == 'https' and address.hostname == 'api.deepseek.com' \
                    and address.port in (None,443) and address.username is None and address.password is None \
                    and not address.query and not address.fragment
            except ValueError: official = False
            if not official:
                report['status']='refused_non_official_endpoint'; persist(); return
            if not key or not model:
                report['status']='missing_model_credentials'; persist(); return
            if not math.isfinite(temperature):
                report['status']='invalid_model_temperature'; persist(); return
            endpoint = base + '/chat/completions'
            device_state.update(control_device=True,chat_mode='device',turn_source='autopilot')
            # Exact static prompt used by GameLoop._run_automatic_turn('autopilot').
            automatic_prompt = (
                '这是用户已开启的自动回合。请结合当前角色与最近对话主动延续情景，自然回应用户，不重复上一轮内容。'
                '依据角色已有性格与动机、对话情景、用户反馈与当前设备状态，自主判断保持现状、调整强度或切换波形。'
                '可以接受、拒绝、暂缓或提出替代回应，不要把用户的话直接当成设备命令；停止与减弱要求仍须优先遵守。'
                '需要调整时使用当前完整波形库；无需调整则 actions 为 []。'
                '不必每轮调整，不必同时操作两个通道，没有画面或声音也不是加大强度的理由。'
            )
            system = build_system_prompt(character,device_state)
            draft = (history + [{'role':'user','content':automatic_prompt}])[-keep:]
            messages = [{'role':'system','content':system}] + [{'role':item['role'],'content':item['content']} for item in draft]
            report.update(system_chars=len(system), messages=message_metadata(messages), endpoint_host='api.deepseek.com')
            report['thinking_disabled'] = bool(getattr(current_llm,'interactive_flash',False))
            report['status']='probing'; persist()

            if production_only:
                # Validate the actual installed client endpoint too, since this
                # path calls that client instead of the independent HTTP probes.
                try:
                    client_address = urlsplit(str(getattr(current_llm,'url','')))
                    official_client = client_address.scheme == 'https' and client_address.hostname == 'api.deepseek.com' \
                        and client_address.port in (None,443) and client_address.username is None and client_address.password is None \
                        and not client_address.query and not client_address.fragment
                except ValueError: official_client = False
                if not official_client:
                    report['status']='refused_non_official_endpoint'; persist(); return
                report.update(production_success_count=0, max_tokens=int(current_llm.max_tokens),
                              request_timeout_s=float(current_llm.timeout_s),production_call_timeout_s=75)
                production_history = copy.deepcopy(draft)
                for turn_index in (1,2):
                    attempt = {'label':'production_call','turn_index':turn_index,'status':'running',
                               'line_chars':0,'action_count':0,'elapsed_ms':None}
                    report['attempts'].append(attempt); persist()
                    started = time.monotonic()
                    try:
                        line,actions = await asyncio.wait_for(current_llm.chat(
                            copy.deepcopy(character),copy.deepcopy(production_history),copy.deepcopy(device_state),
                            image_b64=None),timeout=75)
                        if not isinstance(line,str) or not line.strip() or not isinstance(actions,list):
                            raise ValueError('invalid_model_result')
                        attempt.update(status='success',line_chars=len(line),action_count=len(actions))
                        report['production_success_count'] += 1
                        # Preserve the app's plain-line history representation:
                        # the installed LLM must normalize it at request time.
                        production_history = (production_history + [
                            {'role':'assistant','content':line},
                            {'role':'user','content':automatic_prompt},
                        ])[-keep:]
                    except Exception as failure:
                        attempt.update(status='failed',error_type=type(failure).__name__)
                        metadata = error_metadata(getattr(failure,'diagnostic',None))
                        if metadata: attempt['model_diagnostic'] = metadata
                    finally:
                        attempt['elapsed_ms'] = round((time.monotonic()-started)*1000)
                        persist()
                    if attempt['status'] != 'success': break
                report.update(status='completed' if report['production_success_count'] == 2 else 'production_failed',
                              finished_at=stamp(),output_locked_at_end=bool(live.safety.estop_active),
                              autopilot_disabled_at_end=not loop.autopilot)
                persist(); return

            async def probe(label,probe_messages,json_mode):
                attempt = {'label':label,'json_mode':json_mode,'request_started_at':stamp(),'request_ended_at':None,
                           'http_status':None,'content_chars':0,'non_whitespace_chars':0,'content_type':None,
                           'stripped_starts_object':False,'stripped_ends_object':False,
                           'reasoning_chars':0,'finish_reason':None,'usage':{},
                           'returned_model':None,'system_chars':len(text_content(probe_messages[0].get('content'))),
                           'messages':message_metadata(probe_messages),'usable_content':False}
                report['attempts'].append(attempt); persist()
                started = time.monotonic()
                payload = {'model':model,'messages':probe_messages,'temperature':temperature,'max_tokens':1500}
                if json_mode: payload['response_format']={'type':'json_object'}
                if report['thinking_disabled']: payload['thinking']={'type':'disabled'}
                try:
                    # A fresh connection pool per attempt separates this probe
                    # from the live LLM's client. Redirects are never followed.
                    async with httpx.AsyncClient(timeout=45,trust_env=False,follow_redirects=False) as client:
                        response = await asyncio.wait_for(client.post(endpoint,headers={'Authorization':'Bearer '+key},json=payload),timeout=45)
                    attempt['http_status']=response.status_code
                    if response.status_code == 200:
                        data=response.json()
                        if not isinstance(data,dict): raise ValueError('response_shape')
                        choices=data.get('choices')
                        choice=choices[0] if isinstance(choices,list) and choices and isinstance(choices[0],dict) else {}
                        message=choice.get('message') if isinstance(choice.get('message'),dict) else {}
                        raw_content=message.get('content')
                        content=text_content(raw_content)
                        reasoning=text_content(message.get('reasoning_content'))
                        finish=choice.get('finish_reason')
                        allowed_finish={'stop','length','content_filter','tool_calls','function_call','aborted','insufficient_system_resource',None}
                        parsed=parsed_metadata(content)
                        attempt.update(content_chars=len(content),non_whitespace_chars=sum(not char.isspace() for char in content),
                                       content_type=type(raw_content).__name__,
                                       stripped_starts_object=content.strip().startswith('{'),
                                       stripped_ends_object=content.strip().endswith('}'),reasoning_chars=len(reasoning),
                                       finish_reason=finish if isinstance(finish,(str,type(None))) and finish in allowed_finish else 'unknown',
                                       usage=usage_metadata(data.get('usage')),returned_model=model_name(data.get('model')),
                                       usable_content=bool(content.strip()),
                                       strict_usable_content=parsed['json_object'] and parsed['line_present'],**parsed)
                except Exception as failure:
                    attempt['error_type']=type(failure).__name__
                finally:
                    attempt.update(request_ended_at=stamp(),elapsed_ms=round((time.monotonic()-started)*1000))
                    persist()
                return attempt['usable_content']

            first = await probe('current_history_json',messages,True)
            repair_messages = copy.deepcopy(messages)
            # Match LLM.chat's actual retry prompt without changing live history.
            repair_messages[0]['content'] += (
                '\\n请只返回一个完整 JSON 对象：{"line":"非空的角色回复","actions":[]}。'
                '不要返回思考过程或多个 JSON 对象。'
            )
            second = await probe('current_history_repair_plain',repair_messages,False)
            json_history_messages = copy.deepcopy(messages)
            for message in json_history_messages[1:]:
                if message.get('role') == 'assistant' and isinstance(message.get('content'),str):
                    message['content'] = json.dumps({'line':message['content'],'actions':[]},ensure_ascii=False)
            third = await probe('json_assistant_history_json',json_history_messages,True)
            if not first and not second and not third:
                neutral = [{'role':'system','content':'You are a helpful assistant. Return a JSON object with line (a short greeting) and actions (an empty array).'},
                           {'role':'user','content':'Please greet me briefly in JSON.'}]
                await probe('neutral_no_history_json',neutral,True)
            report.update(status='completed',finished_at=stamp(),output_locked_at_end=bool(live.safety.estop_active),
                          autopilot_disabled_at_end=not loop.autopilot)
            persist()

        if not running:
            report['status']='not_running'; persist()
        else:
            future = asyncio.run_coroutine_threadsafe(diagnose(diagnostic_runtime),diagnostic_runtime.loop)
            try: future.result(timeout=210)
            except Exception as failure:
                future.cancel()
                report.update(status='diagnostic_internal_error',error_type=type(failure).__name__,finished_at=stamp())
                persist()
        diagnostic_status = report['status']
        """;
}
