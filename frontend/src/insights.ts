// The Insights page: an AG Studio report over Rebuttal's disputes, with an AI assistant that can
// answer questions about them and edit the report.
//
// AG Studio itself is loaded from the CDN as the `agStudio` global (see templates/insights.html);
// this file only describes the data and the report, so the bundle stays small. The data comes from
// /api/insights, built by rebuttal/insights.py.

import type {
  AgDataSourcesDefinition,
  AgExpressionFieldDefinition,
  AgFieldDefinition,
  AgFormat,
  AgPageState,
  AgReportState,
  AgStudioApi,
  AgWidgetLayoutState,
} from 'ag-studio';
import { openaiAdapter } from './openai-adapter';

type Dispute = Record<string, string | number | boolean | null>;
type Insights = { asOf: string; generatedHistory: boolean; disputes: Dispute[]; monthly: Dispute[] };
type Config = { dataUrl: string; aiEnabled: boolean; licenseKey: string };

const S = (window as unknown as { agStudio: typeof import('ag-studio') }).agStudio;

// --- data -------------------------------------------------------------------------------

type FieldSpec = [id: string, name: string, format: AgFormat, hide?: boolean];
const USD = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' });
const fields = (specs: FieldSpec[]): AgFieldDefinition[] =>
  specs.map(([id, name, format, hide]) => ({
    id, name, format, ...(hide && { hide }),
    ...(format === 'currencyFormat' && { formatOptions: { format: USD } }),
  }) as AgFieldDefinition);
const sum = (id: string) => ({ id, aggregation: 'sum' as const });
const ratio = (id: string, name: string, description: string, top: string, bottom: string): AgExpressionFieldDefinition => ({
  id, name, description, format: 'percentageFormat', isMeasure: true,
  expression: { operator: 'divide', inputs: [sum(top), sum(bottom)] },
});

function dataSources(d: Insights): AgDataSourcesDefinition {
  const dates = [...d.disputes.map((r) => r.opened as string), ...d.monthly.map((r) => r.month as string)].sort();
  const today = d.asOf.slice(0, 10);
  return {
    description:
      'PayPal disputes for Ember & Clay, a small ceramics studio in Jaipur selling to US buyers, as handled by ' +
      'Rebuttal (an AI dispute agent) or by the owner by hand before Rebuttal. Amounts are USD. ' +
      '"Recovered" is money the seller kept; "Lost" is money refunded plus PayPal dispute fees. ' +
      (d.generatedHistory
        ? 'Rows whose source is "Generated demo history" are illustrative, not real cases.'
        : 'All rows are live cases from the PayPal sandbox.'),
    sources: [
      {
        id: 'disputes',
        description: 'One row per PayPal dispute: reason, money, evidence strength, what Rebuttal recommended, what the seller chose, and the outcome.',
        data: d.disputes,
        fields: fields([
          ['id', 'Dispute ID', 'textFormat'],
          ['opened', 'Opened', 'dateFormat'],
          ['closed', 'Closed', 'dateFormat'],
          ['reason', 'Reason', 'textFormat'],
          ['stage', 'Stage', 'textFormat'],
          ['amount', 'Amount disputed', 'currencyFormat'],
          ['strength', 'Evidence strength', 'textFormat'],
          ['recommended', 'Rebuttal recommended', 'textFormat'],
          ['chosen', 'Seller chose', 'textFormat'],
          ['followedAdvice', 'Followed advice', 'booleanFormat'],
          ['outcome', 'Outcome', 'textFormat'],
          ['recovered', 'Recovered', 'currencyFormat'],
          ['lost', 'Lost', 'currencyFormat'],
          ['fee', 'Dispute fees', 'currencyFormat'],
          ['responseHours', 'Hours to respond', 'decimalFormat'],
          ['handledBy', 'Handled by', 'textFormat'],
          ['source', 'Source', 'textFormat'],
          ['isClosed', 'Closed (1/0)', 'integerFormat', true],
          ['isWon', 'Won (1/0)', 'integerFormat', true],
          ['onTime', 'Answered on time (1/0)', 'integerFormat', true],
          ['answered', 'Answered (1/0)', 'integerFormat', true],
          ['count', 'Disputes', 'integerFormat'],
        ]),
      },
      {
        id: 'monthly',
        name: 'Orders and disputes by month',
        description: 'How many orders the shop took and how many disputes were opened each month.',
        data: d.monthly,
        fields: fields([
          ['month', 'Month', 'dateFormat'],
          ['orders', 'Orders', 'integerFormat'],
          ['disputes', 'Disputes opened', 'integerFormat'],
        ]),
      },
    ],
    relationships: [
      { id: 'disputes-opened-calendar', source: { tableId: 'disputes', fieldId: 'opened' }, target: { calendarId: 'calendar' } },
      { id: 'monthly-calendar', source: { tableId: 'monthly', fieldId: 'month' }, target: { calendarId: 'calendar' } },
    ],
    calendars: [{
      id: 'calendar', label: 'Calendar',
      range: { from: { type: 'date', value: dates[0] ?? today }, to: { type: 'date', value: today } },
      fragments: ['year', 'quarter', 'month', 'week', 'day'],
    }],
    expressions: [
      ratio('winRate', 'Win rate', 'Disputes won / disputes closed.', 'disputes.isWon', 'disputes.isClosed'),
      ratio('onTimeRate', 'Answered on time', 'Disputes answered before PayPal\'s deadline / disputes answered.', 'disputes.onTime', 'disputes.answered'),
      ratio('disputeRate', 'Dispute rate', 'Disputes opened / orders taken. PayPal watches this rate.', 'monthly.disputes', 'monthly.orders'),
    ],
  } as AgDataSourcesDefinition;
}

// --- report -----------------------------------------------------------------------------

const at = (xTrack: number, yTrack: number, xSpan: number, ySpan: number): AgWidgetLayoutState => ({ xTrack, yTrack, xSpan, ySpan });
const title = (text: string) => ({ title: { enabled: true, text } });
const legend = { theme: { common: { legend: { enabled: true } } } };
const kpi = (text: string, value: { id: string; aggregation?: 'sum' }, spark?: string) => ({
  type: 'value' as const,
  dataMapping: { value: [value], ...(spark && { sparklineX: [{ id: spark }] }) },
  format: { title: { enabled: true, text } },
});

const overview: AgPageState = {
  id: 'overview',
  widgets: {
    'kpi-disputed': kpi('Money disputed', sum('disputes.amount'), 'calendar::month'),
    'kpi-recovered': kpi('Kept by the seller', sum('disputes.recovered'), 'calendar::month'),
    'kpi-lost': kpi('Lost to disputes', sum('disputes.lost'), 'calendar::month'),
    'kpi-win': kpi('Win rate', { id: 'winRate' }),
    'kpi-rate': kpi('Dispute rate', { id: 'disputeRate' }),
    'by-month': {
      type: 'combo-chart-grouped-column-line',
      dataMapping: {
        categoryKey: [{ id: 'calendar::month' }],
        valueKey: [sum('monthly.disputes')],
        secondaryValueKey: [{ id: 'disputeRate' }],
      },
      format: { ...title('Disputes opened by month, with the dispute rate'), style: legend },
    },
    'on-time': {
      type: 'radial-gauge',
      dataMapping: { value: [{ id: 'onTimeRate' }] },
      format: { ...title('Answered before the deadline'), style: { scaleLabel: { min: 0, max: 1 }, secondaryLabel: { enabled: true, text: 'of answered disputes' } } },
    },
    'reason-outcome': {
      type: 'column-chart-stacked',
      dataMapping: { categoryKey: [{ id: 'disputes.reason' }], valueKey: [sum('disputes.amount')], legendKey: [{ id: 'disputes.outcome' }] },
      format: { ...title('Money disputed by reason and outcome'), style: legend },
    },
    outcomes: {
      type: 'donut-chart',
      dataMapping: { categoryKey: [{ id: 'disputes.outcome' }], valueKey: [sum('disputes.count')] },
      format: { ...title('Outcomes'), style: { custom: { pieDataLabel: { enabled: true }, totalLabel: { enabled: true } } } },
    },
  },
  widgetLayout: {
    'kpi-disputed': at(0, 0, 5, 7), 'kpi-recovered': at(5, 0, 5, 7), 'kpi-lost': at(10, 0, 5, 7),
    'kpi-win': at(15, 0, 4, 7), 'kpi-rate': at(19, 0, 5, 7),
    'by-month': at(0, 7, 16, 17), 'on-time': at(16, 7, 8, 17),
    'reason-outcome': at(0, 24, 15, 18), outcomes: at(15, 24, 9, 18),
  },
};

const comparison: AgPageState = {
  id: 'before-after',
  widgets: {
    handled: {
      type: 'button-filter',
      dataMapping: { value: [{ id: 'disputes.handledBy' }] },
      format: { ...title('Handled by'), style: { dimensions: { height: 28, minWidth: 64 } } },
    },
    'kpi-win': kpi('Win rate', { id: 'winRate' }),
    'kpi-on-time': kpi('Answered on time', { id: 'onTimeRate' }),
    'kpi-lost': kpi('Lost to disputes', sum('disputes.lost')),
    'win-by-handler': {
      type: 'bar-chart-grouped',
      dataMapping: { categoryKey: [{ id: 'disputes.handledBy' }], valueKey: [{ id: 'winRate' }, { id: 'onTimeRate' }] },
      format: { ...title('Win rate and on-time answers: by hand vs with Rebuttal'), style: legend },
    },
    'lost-pivot': {
      type: 'pivot-grid',
      dataMapping: { rows: [{ id: 'disputes.reason' }], columns: [{ id: 'disputes.handledBy' }], values: [sum('disputes.lost')] },
      format: { ...title('Money lost by reason'), style: { totalColumns: true, grandTotalRow: { enabled: true } } },
    },
  },
  widgetLayout: {
    'kpi-win': at(0, 0, 6, 7), 'kpi-on-time': at(6, 0, 6, 7), 'kpi-lost': at(12, 0, 6, 7), handled: at(18, 0, 6, 7),
    'win-by-handler': at(0, 7, 13, 18), 'lost-pivot': at(13, 7, 11, 18),
  },
};

const advice: AgPageState = {
  id: 'advice',
  widgets: {
    'strength-treemap': {
      type: 'treemap-chart',
      dataMapping: { categoryKey: [{ id: 'disputes.strength' }, { id: 'disputes.outcome' }], valueKey: [sum('disputes.amount')] },
      format: title('Evidence strength and what happened (sized by money)'),
    },
    'advice-pivot': {
      type: 'pivot-grid',
      dataMapping: { rows: [{ id: 'disputes.recommended' }, { id: 'disputes.chosen' }], columns: [{ id: 'disputes.outcome' }], values: [sum('disputes.count')] },
      format: { ...title('Rebuttal\'s advice, the seller\'s choice, and the outcome'), style: { totalColumns: true, grandTotalRow: { enabled: true } } },
    },
    cases: {
      type: 'grid',
      dataMapping: {
        cols: [
          { id: 'disputes.id' }, { id: 'disputes.opened' }, { id: 'disputes.reason' }, { id: 'disputes.strength' },
          { id: 'disputes.recommended' }, { id: 'disputes.chosen' }, { id: 'disputes.outcome' },
          sum('disputes.amount'), sum('disputes.recovered'), sum('disputes.lost'), { id: 'disputes.handledBy' },
        ],
      },
      sort: [{ field: { id: 'disputes.opened' }, direction: 'desc' }],
      format: title('Every dispute, newest first'),
    },
  },
  widgetLayout: { 'strength-treemap': at(0, 0, 10, 20), 'advice-pivot': at(10, 0, 14, 20), cases: at(0, 20, 24, 24) },
};

const report: AgReportState = { selectedPageId: overview.id, pages: [overview, comparison, advice] };
const PAGE_TITLES: Record<string, string> = { overview: 'Overview', 'before-after': 'By hand vs Rebuttal', advice: 'Advice and outcomes' };

// --- theme: Rebuttal's own colour tokens, so Studio follows light and dark mode -----------

const v = (name: string) => `var(--${name})`;
const palette = ['accent', 'blue', 'amber', 'red', 'green', 'muted'].flatMap((c, i) => [
  [`chartPaletteFills${i + 1}Color`, v(c)], [`chartPaletteStrokes${i + 1}Color`, v(c)],
]);
const theme = S.studioTheme.withParams({
  fontFamily: 'Inter, system-ui, sans-serif',
  fontSize: 14,
  textColor: v('ink'), foregroundColor: v('ink'), subtleTextColor: v('muted'),
  backgroundColor: v('card'), borderColor: v('line'), accentColor: v('accent'), rowHoverColor: v('paper'),
  studioWrapperBorder: false, studioWrapperBorderRadius: 0, studioWrapperSpacing: 0,
  studioWrapperBackgroundColor: v('paper'),
  studioCanvasBorder: false, studioCanvasBorderRadius: 0, studioCanvasShadow: false,
  studioCanvasBackgroundColor: v('paper'), studioCanvasPadding: 12, studioCanvasRowHeight: 18,
  studioWidgetBackgroundColor: v('card'), studioWidgetBorder: { width: 1, color: v('line') },
  studioWidgetBorderRadius: 14, studioWidgetPadding: 12,
  studioWidgetTitleFontSize: 15, studioWidgetTitleFontWeight: 650, studioWidgetTitleTextColor: v('ink'),
  studioWidgetSubtitleTextColor: v('muted'), studioWidgetCaptionTextColor: v('muted'),
  ...Object.fromEntries(palette),
} as Record<string, unknown>);

// --- AI assistant ------------------------------------------------------------------------

const PROMPTS = [
  { label: 'Where am I losing money?', prompt: 'Which dispute reasons cost me the most money, and what would cut those losses?' },
  { label: 'Did following the advice pay?', prompt: 'Compare outcomes when I followed Rebuttal\'s recommendation with when I didn\'t.' },
  { label: 'Before and after Rebuttal', prompt: 'Compare win rate, on-time answers and money lost for disputes handled by hand versus by Rebuttal.' },
  { label: 'Chart losses by month', prompt: 'Add a chart of money lost to disputes by month, split by reason.' },
];

// --- start ---------------------------------------------------------------------------------

async function main() {
  const root = document.getElementById('studio')!;
  const config: Config = JSON.parse(document.getElementById('insights-config')!.textContent!);
  if (config.licenseKey) S.AgStudioLicenseManager.setLicenseKey(config.licenseKey);
  const data: Insights = await fetch(config.dataUrl).then((r) => r.json());

  const ai = config.aiEnabled
    ? ({ api }: { api: AgStudioApi }) =>
        S.createAiHarness(api, { adapter: openaiAdapter({ endpoint: '/api/ai' }), promptStarters: PROMPTS })
    : undefined;

  // Page tabs above Studio. They change the page on click, and follow Studio when it (or the AI)
  // changes page itself.
  const tabs = document.getElementById('studio-pages')!;
  const markTab = (id?: string) =>
    tabs.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.page === id));
  const renderTabs = (state: AgReportState) => {
    tabs.innerHTML = (state.pages ?? []).map((p) =>
      `<button type="button" data-page="${p.id}">${PAGE_TITLES[p.id] ?? p.id}</button>`).join('');
    markTab(state.selectedPageId);
  };

  root.innerHTML = '';
  const api = S.createStudio(root, {
    theme,
    layout: { widgetBorderEnabled: true },
    // View mode stays clean; edit mode opens the AI chat on the left and Studio's editors on the right.
    panels: { view: {}, edit: { ...(ai && { left: ['ai'] }), right: ['edit', 'data', 'filters'] } },
    onStudioReady: (e: { api: AgStudioApi }) => renderTabs(e.api.getState()),
    onStateUpdated: (e: { state: AgReportState }) => renderTabs(e.state),
    data: dataSources(data),
    initialState: report,
    mode: 'view',
    ...(ai && { ai }),
  }, { modules: ai ? [S.AgStudioAiModule] : [] });

  tabs.addEventListener('click', (e) => {
    const id = (e.target as HTMLElement).closest('button')?.dataset.page;
    if (id) api.setState({ ...api.getState(), selectedPageId: id });
  });

  const edit = document.getElementById('studio-edit') as HTMLButtonElement;
  const idleLabel = ai ? 'Ask AI or edit report' : 'Edit report';
  edit.textContent = idleLabel;
  edit.hidden = false;
  edit.addEventListener('click', () => {
    const editing = edit.dataset.editing !== '1';
    api.updateProperties({ mode: editing ? 'edit' : 'view' });
    edit.dataset.editing = editing ? '1' : '0';
    edit.textContent = editing ? 'Done' : idleLabel;
  });
  (window as unknown as { studio: AgStudioApi }).studio = api;
}

main().catch((err) => {
  console.error(err);
  const root = document.getElementById('studio');
  if (root) root.innerHTML = `<div class="banner bad">Insights couldn't load: ${String(err.message ?? err)}</div>`;
});
