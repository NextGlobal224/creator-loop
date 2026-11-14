import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

// Exercise the actual app watch function with an isolated fake API/timer.
// No browser, Cloudflare request, or provider invocation is involved.
test('waiting for quota remains cancellable and never schedules a retry', async () => {
  const app = readFileSync(new URL('../public/app.js', import.meta.url), 'utf8');
  const definition = app.match(/^async function watch\(\)\{.*\}$/m)?.[0];
  assert.ok(definition, 'actual watch handler must remain identifiable');
  const elements = new Map();
  const timers = [];
  const messages = [];
  const context = vm.createContext({
    $: key => { if (!elements.has(key)) elements.set(key, {}); return elements.get(key); },
    clearTimeout() {}, setTimeout: (...args) => { timers.push(args); return 1; },
    api: async () => ({status:'WAITING_QUOTA', kind:'SOURCE_TRANSCRIPTION', stage:0, message:'Đang chờ hạn mức'}),
    say: text => messages.push(text), localStorage: {getItem: () => null},
    storageKey:'fixture', sourceReview:{refreshIfCurrent:async () => {}},
    jobId:'fixture', pollTimer:null, jobBusy:false, submitting:false, completedWorkflowId:null,
  });
  await vm.runInContext(`${definition}; watch()`, context);
  assert.equal(timers.length, 0);
  assert.equal(context.jobBusy, true);
  assert.equal(elements.get('#cancel').disabled, false);
  assert.equal(elements.get('#start').disabled, true);
  assert.equal(messages[0], 'Đang chờ hạn mức');
});
