export default function NFTCard() {
  return (
    <div className="card" style={{ textAlign:'center', margin:20, padding:16 }}>
      <p>보유 NFT: <b>1,467</b> pieces</p>
      <p>당일 수익 NFT: <b>10</b> pieces</p>
      <p>락업 수익 NFT: <b>300</b> pieces</p>
      <p style={{ color: '#ff6b00' }}>NFT 1 pieces / 5,000 KRW</p>
    </div>
  )
}