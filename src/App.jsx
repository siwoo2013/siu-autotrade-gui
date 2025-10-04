import React, { useState } from 'react'
import Intro from './pages/Intro'
import Home from './pages/Home'

export default function App() {
  const [showIntro, setShowIntro] = useState(true)
  React.useEffect(() => {
    const timer = setTimeout(() => setShowIntro(false), 1200)
    return () => clearTimeout(timer)
  }, [])
  return showIntro ? <Intro /> : <Home />
}