const vm = require('node:vm');

let raw = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => { raw += chunk; });
process.stdin.on('end', () => {
  let payload;
  try {
    payload = JSON.parse(raw);
  } catch (err) {
    process.stdout.write(JSON.stringify({ ok: false, error: 'invalid input: ' + err.message }));
    return;
  }

  const result = {};
  try {
    for (const [field, entry] of Object.entries(payload)) {
      const context = vm.createContext(Object.create(null));
      const script = new vm.Script('(function(value){' + entry.js + '})');
      const fn = script.runInContext(context, { timeout: 100 });
      const value = fn(entry.value);
      result[field] = value === undefined ? null : value;
    }
    process.stdout.write(JSON.stringify({ ok: true, values: result }));
  } catch (err) {
    process.stdout.write(JSON.stringify({ ok: false, error: String(err && err.message ? err.message : err) }));
  }
});
