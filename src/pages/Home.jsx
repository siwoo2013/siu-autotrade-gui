import React from 'react'
import Header from '../components/Header'
import NFTCard from '../components/NFTCard'
import NavBar from '../components/NavBar'

export default function Home() {
  return (
    <div>
      <Header />
      <div style={{ padding: 20, maxWidth: 860, margin: '0 auto' }}>
        <NFTCard />
      </div>
      <NavBar />
    </div>
  )
}