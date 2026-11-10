import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'
import { createHash } from 'node:crypto'

const root = path.resolve(path.dirname(new URL(import.meta.url).pathname).replace(/^\/([A-Za-z]:)/, '$1'), '../..')
const require = createRequire(path.join(root, 'al1s-frontend/package.json'))
const { parse, compileScript } = require('@vue/compiler-sfc')
const { transform } = require('esbuild')
const ts = require('typescript')
const dir = path.join(root, 'artifacts/local-vision-stop')
const filename = path.join(root, 'al1s-frontend/src/modules/bots/ai/LocalVisionControl.vue')
const { descriptor } = parse(fs.readFileSync(filename, 'utf8'), { filename })
const compiled = compileScript(descriptor, { id: 'data-v-fd204fa6', inlineTemplate: true }).content
const { code } = await transform(compiled, { loader: 'ts', target: 'es2022' })
const tree = ts.createSourceFile('control.js', code, ts.ScriptTarget.Latest, true)
const aliases = { defineComponent: 'xe', ref: 'b', computed: 'te', onMounted: 'Oe', onBeforeUnmount: 'Ue',
  openBlock: 'l', createElementBlock: 'o', createElementVNode: 'e', createTextVNode: 'h',
  normalizeClass: 'we', toDisplayString: 'r', createCommentVNode: 'w', apiClient: 'de' }
const bindings = [], edits = []
for (const node of tree.statements) {
  if (ts.isImportDeclaration(node)) {
    for (const binding of node.importClause.namedBindings.elements) {
      const name = binding.propertyName?.text ?? binding.name.text
      if (!aliases[name]) throw new Error('unmapped Vue binding: ' + name)
      bindings.push(`${binding.name.text}=${aliases[name]}`)
    }
    edits.push([node.getFullStart(), node.end, ''])
  } else if (ts.isExportAssignment(node)) {
    edits.push([node.getStart(tree), node.expression.getStart(tree), 'return '])
  }
}
let body = code
for (const [start, end, value] of edits.sort((a,b) => b[0]-a[0])) body = body.slice(0,start)+value+body.slice(end)
const source = fs.readFileSync(path.join(dir, 'original.js'), 'utf8')
if (createHash('sha256').update(source.replace(/\r\n/g, '\n')).digest('hex') !== '09de9202e18dbc85c8b395b1d0b9a65e46eaaab0d28e2f263f0780f0e6f816b0') {
  throw new Error('This component patch requires the verified original platform module')
}
const original = ts.createSourceFile('runtime.js', source, ts.ScriptTarget.Latest, true)
let replacement
function locate(node) {
  if (ts.isVariableDeclaration(node) && node.name.getText(original) === 'Cc' && node.initializer?.getText(original).includes('__name:"LocalVisionControl"')) {
    if (replacement) throw new Error('duplicate control component')
    replacement = node.initializer
  }
  ts.forEachChild(node, locate)
}
locate(original)
if (!replacement) throw new Error('current local vision component not found')
const output = source.slice(0,replacement.getStart(original)) + `(()=>{const ${bindings.join(',')};${body}})()` + source.slice(replacement.end)
const meta = JSON.parse(fs.readFileSync(path.join(dir, 'runtime.json'), 'utf8'))
fs.mkdirSync(path.join(dir, 'payload/assets'), { recursive: true })
fs.writeFileSync(path.join(dir, 'payload/assets', meta.module), output)
console.log(JSON.stringify({ module: meta.module, replaced_component: 'LocalVisionControl', other_component_source_unchanged: true }))
