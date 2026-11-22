export const reconcileViewTemplate = `
  <div v-show="tab===4" class="panel">
    <h2>对账互证</h2>
    <p :class="recon.all_ok ? 'ok' : 'err'">{{ recon.all_ok ? '全部同源' : '存在分裂' }}</p>
    <p class="ok" v-if="recon.order_ok">占位序按过期时刻同态（{{ recon.sort }}）</p>
    <p class="err" v-else>占位序与过期时刻不一致</p>
    <table>
      <thead><tr><th>档</th><th>盘面已订</th><th>正式册席</th><th>结果</th></tr></thead>
      <tbody>
        <tr v-for="i in (recon.items||[])" :key="i.slot_code">
          <td>{{ i.slot_code }}</td><td>{{ i.slot_booked }}</td>
          <td>{{ i.booking_seats }}</td>
          <td :class="i.ok?'ok':'err'">{{ i.ok ? '通过' : '分裂' }}</td>
        </tr>
      </tbody>
    </table>
    <h3>占位序（过期时刻）</h3>
    <table>
      <thead><tr><th>序</th><th>ID</th><th>当事人</th><th>过期</th></tr></thead>
      <tbody>
        <tr v-for="h in (recon.holds||[])" :key="h.id">
          <td>{{ h.order_seq }}</td><td>{{ h.id }}</td><td>{{ h.party_key }}</td><td>{{ h.expire_at }}</td>
        </tr>
      </tbody>
    </table>
  </div>
`;
