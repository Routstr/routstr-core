import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import ts from 'typescript';

function loadSource(path, imports) {
  const source = readFileSync(new URL(path, import.meta.url), 'utf8');
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2020,
      jsx: ts.JsxEmit.ReactJSX,
    },
    fileName: fileURLToPath(new URL(path, import.meta.url)),
  });
  const sourceModule = { exports: {} };
  vm.runInNewContext(outputText, {
    module: sourceModule,
    exports: sourceModule.exports,
    require(name) {
      assert.ok(name in imports, `Unexpected import ${name}`);
      return imports[name];
    },
  });
  return sourceModule.exports;
}

function runnerHarness() {
  const calls = [];
  const pending = [];
  const slots = [];
  const cleanups = [];
  let cursor = 0;
  const react = {
    useState(initial) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = initial;
      return [
        slots[index],
        (next) => {
          slots[index] = next;
        },
      ];
    },
    useRef(initial) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = { current: initial };
      return slots[index];
    },
    useEffect(effect) {
      const index = cursor++;
      if (!(index in slots)) {
        slots[index] = true;
        cleanups.push(effect());
      }
    },
    useCallback: (fn) => fn,
  };
  const exports = loadSource('./use-provider-certification-runner.ts', {
    react,
    '@/lib/api/services/admin': {
      AdminService: {
        certifyProvider(providerId, options) {
          calls.push({ providerId, ...options });
          return new Promise((resolve, reject) =>
            pending.push({ resolve, reject })
          );
        },
      },
    },
    '@/lib/provider-certification': {
      getErrorMessage: (error) => error.message,
    },
  });
  return {
    ...exports,
    calls,
    pending,
    render() {
      cursor = 0;
      return exports.useProviderCertificationRunner(1);
    },
    unmount() {
      cleanups.forEach((cleanup) => cleanup?.());
    },
  };
}

const model = (modelId, paths = ['default']) => ({
  modelId,
  targets: paths.map((path) => ({ path, label: path })),
});
const report = { rows: [] };
const flush = () => new Promise((resolve) => setImmediate(resolve));

test('reset cancels queued paths/models and blocks restart until in-flight work finishes', async () => {
  const harness = runnerHarness();
  const hook = harness.render();
  const old = hook.run([model('first', ['a', 'b']), model('second')], false);
  assert.equal(harness.render().isPending, true);
  hook.reset();
  assert.equal(harness.render().isPending, true);
  await hook.run([model('restart')], false);
  assert.equal(harness.calls.length, 1);
  harness.pending[0].resolve(report);
  await old;
  const finished = harness.render();
  assert.equal(finished.isPending, false);
  assert.equal(finished.progress, null);
  assert.equal(finished.results.length, 0);
  assert.deepEqual(
    harness.calls.map((call) => call.model_id),
    ['first']
  );
  const fresh = finished.run([model('restart')], false);
  harness.pending[1].resolve(report);
  await fresh;
  assert.deepEqual(
    harness.calls.map((call) => call.model_id),
    ['first', 'restart']
  );
});

test('unmount cancels queued requests and suppresses stale result updates', async () => {
  const harness = runnerHarness();
  const old = harness.render().run([model('first'), model('second')], false);
  harness.unmount();
  harness.pending[0].resolve(report);
  await old;
  assert.equal(harness.calls.length, 1);
  assert.equal(harness.render().results.length, 0);
});

test('cancellation before dispatch makes no request or progress callback', async () => {
  const harness = runnerHarness();
  const progress = [];
  const result = await harness.runProviderCertification({
    providerId: 1,
    modelRuns: [model('first')],
    includeCache: false,
    shouldContinue: () => false,
    onProgress: (next) => progress.push(next),
  });
  assert.equal(result.length, 0);
  assert.equal(harness.calls.length, 0);
  assert.equal(progress.length, 0);
});

test('per-route failures remain isolated and normal runs retain completed results', async () => {
  const harness = runnerHarness();
  const done = harness.render().run([model('first', ['a', 'b'])], true);
  harness.pending[0].reject(new Error('route failed'));
  await flush();
  harness.pending[1].resolve(report);
  await done;
  const finished = harness.render();
  assert.equal(finished.isPending, false);
  assert.equal(finished.results.length, 2);
  assert.equal(finished.results[0].error, 'route failed');
  assert.equal(finished.results[1].report, report);
  assert.equal(harness.calls[1].check_cache, true);
});

test('dialog close cancels synchronously before notifying its owner', () => {
  const events = [];
  const jsx = (type, props) => ({ type, props });
  const components = new Proxy({}, { get: (_, name) => name });
  const imports = {
    react: {
      useState: (initial) => [
        typeof initial === 'function' ? initial() : initial,
        () => {},
      ],
      useEffect: (effect) => effect(),
    },
    'react/jsx-runtime': { jsx, jsxs: jsx },
    '@tanstack/react-query': { useQuery: () => ({}) },
    'lucide-react': components,
    '@/components/ui/dialog': components,
    '@/components/ui/button': components,
    '@/components/ui/badge': components,
    '@/components/ui/tabs': components,
    '@/components/provider-certification-results': components,
    '@/components/provider-certification-setup': {
      ...components,
      ProviderCertificationSetupPanel: 'SetupPanel',
      getCertificationModelNames: () => ({}),
    },
    '@/hooks/use-provider-certification-runner': {
      useProviderCertificationRunner: () => ({
        results: [],
        progress: null,
        isPending: false,
        run: () => {},
        reset: () => events.push('reset'),
      }),
    },
    '@/lib/api/services/admin': { AdminService: {} },
    '@/lib/provider-certification': {
      emptyCertificationSetup: () => ({
        selectedModelIds: [],
        checkCache: false,
      }),
      buildModelRuns: () => [],
      countCertificationTargets: () => 0,
      getModelsNeedingPath: () => [],
    },
  };
  const { ProviderCertificationDialog } = loadSource(
    '../components/provider-certification-dialog.tsx',
    imports
  );
  const dialog = ProviderCertificationDialog({
    open: true,
    provider: { id: 1, provider_type: 'generic' },
    onOpenChange: () => events.push('owner'),
  });
  dialog.props.onOpenChange(false);
  assert.deepEqual(events, ['reset', 'owner']);
});

test('multi-provider page guards duplicate starts and cancels queued work on unmount', async () => {
  const harness = runnerHarness();
  const cleanups = [];
  const updates = [];
  let stateIndex = 0;
  const setup = {
    selectedModelIds: ['first', 'second'],
    pathModes: {},
    selectedModelPaths: {},
    checkCache: false,
  };
  const initialStates = [[1], 1, { 1: setup }];
  const react = {
    useState(initial) {
      const index = stateIndex++;
      return [
        index < 3 ? initialStates[index] : initial,
        (next) => updates.push(next),
      ];
    },
    useMemo: (fn) => fn(),
    useRef: (initial) => ({ current: initial }),
    useEffect: (effect) => cleanups.push(effect()),
  };
  const helpers = loadSource('../lib/provider-certification.ts', {
    '@/lib/api/errors': { getApiErrorMessage: () => '' },
  });
  const jsx = (type, props) => ({ type, props });
  const components = new Proxy({}, { get: (_, name) => name });
  const imports = {
    react,
    'react/jsx-runtime': { jsx, jsxs: jsx },
    'next/link': { default: 'Link' },
    '@tanstack/react-query': {
      useQuery: () => ({ data: [{ id: 1, provider_type: 'generic' }] }),
      useQueries: () => [{ data: { certification_paths: {} } }],
    },
    'lucide-react': components,
    '@/components/provider-certification-results': {
      summarizeCertificationResults: () => ({}),
      ProviderCertificationResults: 'Results',
    },
    '@/components/provider-certification-setup': {
      getCertificationModelNames: () => ({}),
      ProviderCertificationSetupPanel: 'Setup',
    },
    '@/hooks/use-provider-certification-runner': harness,
    '@/lib/api/services/admin': { AdminService: {} },
    '@/lib/provider-certification': helpers,
    '@/lib/utils': { cn: () => '' },
  };
  for (const name of [
    'app-page-shell',
    'page-header',
    'ui/badge',
    'ui/button',
    'ui/card',
    'ui/checkbox',
    'ui/command',
    'ui/popover',
    'ui/select',
    'ui/tabs',
  ])
    imports[`@/components/${name}`] = components;
  const { default: Page } = loadSource(
    '../app/providers/certification/page.tsx',
    imports
  );
  const nodes = [];
  const visit = (node) => {
    if (!node || typeof node !== 'object') return;
    if (Array.isArray(node)) return node.forEach(visit);
    nodes.push(node);
    visit(node.props?.children);
  };
  visit(Page());
  const runAll = nodes.find(
    (node) => node.props?.onClick?.name === 'runAllProviders'
  );
  assert.ok(runAll);
  runAll.props.onClick();
  runAll.props.onClick();
  assert.equal(harness.calls.length, 1);
  cleanups.forEach((cleanup) => cleanup?.());
  const beforeCompletion = updates.length;
  harness.pending[0].resolve(report);
  await flush();
  assert.equal(harness.calls.length, 1);
  assert.equal(updates.length, beforeCompletion);
});

test('selected-provider aggregate excludes deselected providers and restores them on reselection', () => {
  const { getSelectedCertificationResults } = loadSource(
    '../lib/provider-certification.ts',
    {
      '@/lib/api/errors': { getApiErrorMessage: () => '' },
    }
  );
  const a = { providerId: 1, resultKey: 'a' };
  const results = { 1: [a] };
  const selected = getSelectedCertificationResults([2], results);
  assert.equal(selected.length, 0);
  assert.equal(Math.max(1 - selected.length, 0), 1);
  assert.deepEqual([...getSelectedCertificationResults([1, 2], results)], [a]);
});
