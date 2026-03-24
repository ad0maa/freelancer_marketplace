class CreateBookings < ActiveRecord::Migration[8.1]
  def change
    create_table :bookings do |t|
      t.references :client, null: false, foreign_key: true
      t.references :freelancer, null: false, foreign_key: true
      t.date :start_date, null: false
      t.date :end_date, null: false
      t.decimal :total_amount, precision: 10, scale: 2, null: false
      t.integer :status, default: 0, null: false

      t.timestamps
    end
  end
end
