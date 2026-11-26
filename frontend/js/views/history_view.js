import { api } from "../api.js";
const { ref, onMounted } = Vue;
export const HistoryView = {
  setup() {
    const items = ref([]); const err = ref(""); const voidOnly = ref(false); const voidCount = ref(0);
    async function reload() {
      try {
        const qs = voidOnly.value ? "?void_only=1" : "";
        const res = await api("/api/history" + qs);
        items.value = res.items || []; voidCount.value = res.void_count || 0;
      }
      catch (e) { err.value = e.message || String(e); }
    }
    function toggleVoid() { voidOnly.value = !voidOnly.value; reload(); }
    onMounted(reload);
    return { items, err, voidOnly, voidCount, reload, toggleVoid };
  },
  template: `
  <div class="panel">
    <h2>变更历史</h2>
    <p class="hint">超龄影子作废（release_shadow_void）与求值联查作废都在此留痕，和发布审计同源。</p>
    <p>
      <button class="btn secondary" @click="toggleVoid">{{ voidOnly ? '显示全部' : '只看作废事件' }}</button>
      <span class="pill danger" style="margin-left:.6rem" v-if="voidCount">累计作废事件 {{ voidCount }}</span>
      <button class="btn secondary" style="margin-left:.6rem" @click="reload">刷新</button>
    </p>
    <div class="err" v-if="err">{{ err }}</div>
    <table>
      <thead><tr><th>时间动作</th><th>开关</th><th>环境</th><th>详情</th><th>操作者</th></tr></thead>
      <tbody>
        <tr v-for="it in items" :key="it.id" :style="(it.action||'').includes('void') ? 'background:rgba(240,113,120,0.08)' : ''">
          <td>
            <span :class="(it.action||'').includes('void') ? 'pill danger' : ''">{{ it.action_label || it.action }}</span>
          </td>
          <td>{{ it.flag_key || '-' }}</td><td>{{ it.env_code || '-' }}</td>
          <td>{{ it.detail }}</td><td>{{ it.actor }}</td>
        </tr>
      </tbody>
    </table>
  </div>`
};
