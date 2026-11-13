// Used only with a disposable test home and vetted, synthetic hook sources.
// Startup dialogs may precede or follow the first composer frame. Never wait
// for a particular placeholder, model label, or ordering before handling them.
export function startupAction(output, { running, requests, coolingDown = false }) {
  if (output.includes('Hooks need review')) return 'trust-picker';
  if (output.includes('t trust all')) return 'trust-browser';
  if (output.includes('Lifecycle hooks from config') || output.includes('SessionStart hooks')) return 'close-browser';
  if (requests || !running || coolingDown || output.includes('Waiting for startup')) return 'wait';
  return 'seed';
}
