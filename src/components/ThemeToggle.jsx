export default function ThemeToggle() {
  const isDark = document.documentElement.classList.contains('dark')

  const toggle = () => {
    const root = document.documentElement
    const next = root.classList.toggle('dark')
    localStorage.setItem('theme', next ? 'dark' : 'light')
    const meta = document.querySelector('meta[name="theme-color"]')
    if (meta) meta.setAttribute('content', next ? '#0f1115' : '#f5f6f8')
  }

  return (
    <button onClick={toggle} aria-label="Toggle theme">
      {isDark ? '🌙 Dark' : '☀️ Light'}
    </button>
  )
}