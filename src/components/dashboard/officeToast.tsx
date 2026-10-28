import { useSyncExternalStore } from 'react'
import {
  getToastKind,
  getToastSnapshot,
  subscribeToast,
} from '@/store/officeUiStore'

export function OfficeToast() {
  const message = useSyncExternalStore(subscribeToast, getToastSnapshot)
  const kind = useSyncExternalStore(subscribeToast, getToastKind)
  if (!message) return null
  return <div className={`office-toast toast-${kind}`}>{message}</div>
}
