# The iOS app

The Capacitor shell around the built dashboard, for iPhone and iPad. Build
it with Xcode 16 or later: from `dashboard/`, `npm run build && npx cap
sync ios`, then open `ios/App/App.xcodeproj` or run `xcodebuild` on the
`App` scheme. Push notifications need the Firebase client config. Copy
`App/App/GoogleService-Info.plist.example` to
`App/App/GoogleService-Info.plist` with your Firebase project's values.
Without it the app runs without push. `OtoDockShell/` holds the
shell's rules with their tests (`swift test`).
