export const reconcileViewTemplate = `
<div class="card wide" v-show="tab===4">
  <h2>物化对账</h2>
  <p class="muted">
    世代口径：{{recon.doctrine_path || '-'}} · {{recon.doctrine || '按当前世代过滤，跨代行整笔排除'}}
  </p>
  <p>
    窗口 <b>{{recon.name}}</b> · 当前世代 <b class="gen">g{{recon.window_generation}}</b> ·
    窗内事件 {{recon.window_events}} / 金额 {{recon.window_amount}} ·
    迟到桶 {{recon.late_bucket}} · 拒收 {{recon.rejects}} ·
    对齐 <b :class="recon.aligned?'ok':'late'">{{recon.aligned}}</b>
  </p>
  <p class="muted" v-if="recon.stale_materialization">
    最新物化是 g{{recon.stale_materialization.generation}}（跨代），已整笔排除；
    当前世代物化：{{recon.materialized_generation == null ? '无' : 'g'+recon.materialized_generation}}
  </p>
  <table>
    <thead><tr><th>窗口</th><th>状态</th><th>当前世代</th><th>窗内笔数</th><th>物化世代</th><th>跨代遗留</th><th>迟到</th><th>对齐</th></tr></thead>
    <tbody>
      <tr v-for="r in reconAll" :key="r.window_id">
        <td>{{r.name}}</td>
        <td>{{r.status}}</td>
        <td class="gen">g{{r.window_generation}}</td>
        <td>{{r.window_events}}</td>
        <td>{{r.materialized_generation == null ? '无' : 'g'+r.materialized_generation}}</td>
        <td :class="r.stale_generation != null ? 'late' : ''">{{r.stale_generation == null ? '—' : 'g'+r.stale_generation}}</td>
        <td>{{r.late_bucket}}</td>
        <td :class="r.aligned===true?'ok':(r.contended?'late':'')">{{r.contended ? '争用中' : r.aligned}}</td>
      </tr>
    </tbody>
  </table>
  <div class="err" v-if="reconContended">争用：后到只记争用，未返回对账结果</div>
  <div class="err">{{actionErr}}</div>
</div>
`;
