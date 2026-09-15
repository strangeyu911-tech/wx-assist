import { useEffect, useRef, useState } from 'react'
import { ArrowLeft, CheckCircle, QrCode, SignOut, TestTube, WarningCircle } from '@phosphor-icons/react'
import { QRCodeSVG } from 'qrcode.react'
import { API_BASE } from './SharedComponents'

// 扫码任务的本地暂存 key：离开配置页再回来时恢复未完成的绑定轮询，
// 避免"生成二维码后切走 tab → 没人轮询 → 扫码成功也无人保存"的断链。
const STORAGE_KEY = 'wxassist_onboard_task'
const TASK_TTL_MS = 9.5 * 60 * 1000 // QQ 服务端任务约 5 分钟过期，留足余量
const TERMINAL_STATES = ['completed', 'expired', 'denied', 'cancelled', 'failed']

function QRPreview({ value, label }) {
  if (!value) return <span className="text-xs text-text-muted">二维码地址未返回</span>
  if (/^(data:image\/|https?:\/\/.*\.(png|jpe?g|gif|webp)(\?.*)?$)/i.test(value)) {
    return <img src={value} alt={`${label}扫码二维码`} className="max-h-full max-w-full" />
  }
  return <QRCodeSVG value={value} size={192} level="M" includeMargin bgColor="#ffffff" fgColor="#111827" />
}

export default function PlatformConfigDemo({ platform, initialConfig, onBack, onSaved }) {
  const label = platform === 'qqbot' ? 'QQ' : '飞书'
  const isQQ = platform === 'qqbot'
  const [onboard, setOnboard] = useState(null)
  const [config, setConfig] = useState(initialConfig || {})
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [testSending, setTestSending] = useState(false)
  const isBound = Boolean(config.bound || config.user_openid || config.open_id || config.default_target)

  // onSaved 通过 ref 使用：父组件（概览 5s 轮询）每次渲染都会换新函数，
  // 若直接放进 effect 依赖会反复拆建轮询定时器。
  const savedRef = useRef(onSaved)
  useEffect(() => { savedRef.current = onSaved })

  useEffect(() => {
    setConfig(initialConfig || {})
    setError('')
    setMessage('')
    // 恢复未完成的扫码任务（切换 tab / 刷新页面后回来可继续）
    let restored = null
    try {
      const raw = localStorage.getItem(STORAGE_KEY)
      if (raw) {
        const saved = JSON.parse(raw)
        if (saved?.platform === platform && saved?.task_id && Date.now() - saved.ts < TASK_TTL_MS) {
          restored = { task_id: saved.task_id, qr_url: saved.qr_url || '', status: 'pending' }
        } else {
          localStorage.removeItem(STORAGE_KEY)
        }
      }
    } catch {}
    setOnboard(restored)
  }, [platform, initialConfig])

  function persistTask(task) {
    try {
      if (task?.task_id && !TERMINAL_STATES.includes(task.status)) {
        localStorage.setItem(STORAGE_KEY, JSON.stringify({
          platform, task_id: task.task_id, qr_url: task.qr_url || '', ts: Date.now(),
        }))
      } else {
        localStorage.removeItem(STORAGE_KEY)
      }
    } catch {}
  }

  async function startOnboarding() {
    setBusy(true); setError(''); setMessage('')
    try {
      const res = await fetch(`${API_BASE}/api/platforms/${platform}/onboard/start`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: isQQ ? '{}' : JSON.stringify({ domain: 'feishu' }),
      })
      const data = await res.json()
      if (!res.ok || !data.ok || !data.qr_url) throw new Error(data.error || '平台没有返回有效二维码地址')
      const task = { ...data, status: 'pending' }
      setOnboard(task)
      persistTask(task)
    } catch (err) { setError(err.message || '无法创建扫码任务') } finally { setBusy(false) }
  }

  async function cancelOnboarding() {
    if (!onboard?.task_id) return
    await fetch(`${API_BASE}/api/platforms/${platform}/onboard/${onboard.task_id}/cancel`, { method: 'POST' }).catch(() => {})
    setOnboard(null)
    persistTask(null)
  }

  async function sendTestMessage() {
    setTestSending(true); setError(''); setMessage('正在发送测试消息...')
    try {
      const res = await fetch(`${API_BASE}/api/platforms/${platform}/test-message`, { method: 'POST' })
      const data = await res.json()
      if (!res.ok || !data.ok) throw new Error(data.error || '测试消息发送失败')
      setMessage(`测试消息已发送${data.message_id ? `（消息 ID: ${data.message_id}）` : ''}，请检查${label}客户端`)
    } catch (err) { setError(err.message || '测试消息发送失败'); setMessage('') }
    finally { setTestSending(false) }
  }

  async function unbind() {
    setBusy(true); setError(''); setMessage('')
    try {
      const res = await fetch(`${API_BASE}/api/platforms/${platform}/unbind`, { method: 'POST' })
      const data = await res.json()
      if (!res.ok || !data.ok) throw new Error(data.error || '解除绑定失败')
      setConfig({})
      setOnboard(null)
      persistTask(null)
      setMessage('已解除绑定，平台配置已清空')
      savedRef.current?.()
    } catch (err) { setError(err.message || '解除绑定失败') } finally { setBusy(false) }
  }

  useEffect(() => {
    if (!onboard?.task_id || TERMINAL_STATES.includes(onboard.status)) return undefined
    let failures = 0
    const timer = window.setInterval(async () => {
      try {
        const res = await fetch(`${API_BASE}/api/platforms/${platform}/onboard/${onboard.task_id}/status`)
        const data = await res.json()
        if (!res.ok || data.ok === false) {
          // 后端明确报错：连续 3 次后停止轮询并展示错误，避免无限"连接中"
          failures += 1
          if (failures >= 3) {
            const task = { ...onboard, status: 'failed' }
            setOnboard(task)
            persistTask(task)
            setError(data.error || `扫码状态查询失败（HTTP ${res.status}）`)
          }
          return
        }
        failures = 0
        if (data.status === 'completed') {
          setOnboard(data)
          persistTask(data)
          setConfig(prev => ({
            ...prev,
            ...(isQQ ? {
              app_id: data.app_id || prev.app_id,
              client_secret: data.client_secret || prev.client_secret,
              user_openid: data.user_openid || prev.user_openid,
              bound: true,
            } : {
              app_id: data.app_id || prev.app_id,
              app_secret: data.app_secret || prev.app_secret,
              open_id: data.open_id || prev.open_id,
              bound: true,
            }),
          }))
          setMessage(data.applied === false ? '扫码成功，配置已保存；启动助手后生效' : '扫码绑定成功，平台已自动启用')
          setError('')
          savedRef.current?.()
        } else if (TERMINAL_STATES.includes(data.status)) {
          setOnboard(data)
          persistTask(data)
          setError(data.status === 'expired'
            ? '二维码已过期（QQ 任务约 5 分钟有效），请取消后重新生成'
            : data.error || '扫码任务已结束')
        } else setOnboard(prev => ({ ...prev, ...data }))
      } catch (err) {
        failures += 1
        if (failures >= 3) {
          const task = { ...onboard, status: 'failed' }
          setOnboard(task)
          persistTask(task)
          setError(err.message || '扫码状态查询失败')
        }
      }
    }, platform === 'qqbot' ? 2000 : 5000)
    return () => window.clearInterval(timer)
  }, [onboard?.task_id, onboard?.status, platform, isQQ])

  const statusText = onboard?.status === 'completed' ? '扫码绑定成功'
    : onboard?.status === 'failed' ? '绑定失败，请取消后重新生成二维码'
    : onboard?.status === 'expired' ? '二维码已过期，请取消后重新生成'
    : onboard?.status === 'scaned' ? '已扫码，请在手机上确认'
    : '等待扫码授权...'

  return <div className="space-y-6">
    <div className="flex items-start gap-3">
      <button type="button" onClick={onBack} className="mt-0.5 rounded-lg p-1.5 text-text-muted hover:bg-bg-raised hover:text-text-main"><ArrowLeft size={18} /></button>
      <div><h4 className="text-[15px] font-semibold text-text-main">{label}绑定</h4><p className="mt-1 text-xs text-text-muted">仅支持扫码绑定，绑定后自动启用平台推送。生成二维码后请停留在本页等待绑定完成。</p></div>
    </div>
    {isBound && !onboard ? <div className="space-y-4 rounded-xl border border-border-main bg-bg-raised p-5">
      <div className="flex items-start gap-3 rounded-xl border border-brand-green/20 bg-brand-green-light/30 px-4 py-3"><CheckCircle size={20} weight="fill" className="mt-0.5 shrink-0 text-brand-green" /><div><p className="text-sm font-semibold text-text-main">已绑定</p><p className="mt-1 break-all text-xs text-text-muted">{isQQ ? `Bot ID: ${config.app_id || '—'} · 用户: ${config.user_openid || '—'}` : `应用: ${config.app_id || '—'} · 用户: ${config.open_id || '—'}`}</p></div></div>
      <div className="flex flex-wrap gap-2"><button type="button" disabled={testSending || busy} onClick={sendTestMessage} className="inline-flex items-center gap-1.5 rounded-full bg-brand-green-hover px-4 py-2 text-xs font-semibold text-white disabled:opacity-50"><TestTube size={14} />{testSending ? '发送中...' : '发送测试消息'}</button><button type="button" disabled={busy} onClick={unbind} className="inline-flex items-center gap-1.5 rounded-full border border-border-main px-4 py-2 text-xs font-semibold text-text-muted hover:text-status-error disabled:opacity-50"><SignOut size={14} />解除绑定</button></div>
    </div> : <div className="space-y-4 rounded-xl border border-border-main bg-bg-raised p-5 text-center">
      {!onboard ? <><QrCode size={28} className="mx-auto text-brand-green" /><p className="text-sm font-medium text-text-main">使用{label}扫码绑定</p><p className="text-xs text-text-muted">二维码由{label}官方绑定地址生成，请使用对应客户端扫描。</p><button type="button" disabled={busy} onClick={startOnboarding} className="rounded-lg bg-brand-green px-4 py-2.5 text-sm font-semibold text-white hover:bg-brand-green-hover disabled:opacity-50">{busy ? '创建中...' : '生成二维码'}</button></> : <><div className="mx-auto flex h-52 w-52 items-center justify-center rounded-lg border border-border-main bg-white p-2"><QRPreview value={onboard.qr_url} label={label} /></div><p className="text-sm text-text-main">{statusText}</p>{onboard.user_code && <p className="text-xs text-text-muted">验证码：{onboard.user_code}</p>}{onboard.status !== 'completed' && <button type="button" onClick={cancelOnboarding} className="rounded-lg border border-border-main px-4 py-2 text-sm text-text-muted">取消</button>}</>}
    </div>}
    {(message || error) && <div className={`flex items-center gap-2 text-xs ${error ? 'text-status-error' : 'text-brand-green'}`}>{error ? <WarningCircle size={16} /> : <CheckCircle size={16} weight="fill" />}{error || message}</div>}
  </div>
}
