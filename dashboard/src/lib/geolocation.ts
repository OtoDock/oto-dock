import { Capacitor } from '@capacitor/core'

export interface DeviceLocation {
  lat: number
  lng: number
  accuracy: number
}

/**
 * Get the device's current GPS location.
 *
 * - Android (Capacitor native): uses @capacitor/geolocation plugin with native permissions
 * - Web browser: uses navigator.geolocation API with browser permission prompt
 *
 * Both platforms remember the permission grant — no re-prompt on subsequent calls.
 */
export async function getDeviceLocation(): Promise<DeviceLocation> {
  if (Capacitor.isNativePlatform()) {
    const { Geolocation } = await import('@capacitor/geolocation')
    const pos = await Geolocation.getCurrentPosition({
      enableHighAccuracy: true,
      timeout: 15000,
    })
    return {
      lat: pos.coords.latitude,
      lng: pos.coords.longitude,
      accuracy: pos.coords.accuracy,
    }
  }

  // Web browser
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) {
      reject(new Error('Geolocation not supported'))
      return
    }
    navigator.geolocation.getCurrentPosition(
      (pos) =>
        resolve({
          lat: pos.coords.latitude,
          lng: pos.coords.longitude,
          accuracy: pos.coords.accuracy,
        }),
      (err) => {
        const msgs: Record<number, string> = {
          1: 'Location permission denied',
          2: 'Location unavailable',
          3: 'Location request timed out',
        }
        reject(new Error(msgs[err.code] || 'Could not determine location'))
      },
      {
        enableHighAccuracy: true,
        timeout: 15000,
        maximumAge: 30000, // Accept cached position up to 30s old
      },
    )
  })
}
