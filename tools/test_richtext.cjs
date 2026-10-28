const fs = require('fs');
const path = require('path');
const vm = require('vm');
const root = path.resolve(__dirname, '..');
const context = {URL, document: {compatMode:'CSS1Compat', addEventListener(){}}};
context.window = context;
vm.createContext(context);
for (const file of ['vendor/markdown-it/markdown-it.min.js','vendor/katex/katex.min.js','richtext.js']) {
  vm.runInContext(fs.readFileSync(path.join(root,'shared/web',file),'utf8'), context);
}
const render = source => context.RichText.render(source,'lesson');
const checks = [
  ['inline fractions and subscripts', render('$x_i + \\frac{a}{b}$').includes('katex')],
  ['display matrix', render('$$\n\\begin{pmatrix}a&b\\\\c&d\\end{pmatrix}\n$$').includes('katex-display')],
  ['formula table', render('| value |\n|---|\n| $x^2$ |').includes('<table>')],
  ['raw HTML disabled', !/<script>/.test(render('<script>alert(1)</script>'))],
  ['script image disabled', !/<img[^>]+onerror/.test(render('![x](x"onerror="alert)'))],
  ['local file image disabled', !/<img/.test(render('![x](file:///private.png)'))],
  ['external image opt-in', render('![x](https://example.com/a.png)').includes('data-remote-image')],
  ['private image rejected', !render('![x](https://127.0.0.1/a.png)').includes('data-remote-image')],
  ['unsafe math bounded', render('$\\newcommand{\\a}{\\a}\\a$').includes('math-error')],
  ['code is not math', !render('`$x$`').includes('katex')],
];
for (const [name, passed] of checks) {
  console.log(`${passed ? 'PASS' : 'FAIL'} ${name}`);
  if (!passed) process.exitCode = 1;
}
