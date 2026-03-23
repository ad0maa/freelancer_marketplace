class CreateFreelancers < ActiveRecord::Migration[8.1]
  def change
    create_table :freelancers do |t|
      t.string :name, null: false
      t.string :email, null: false
      t.decimal :hourly_rate,  precision: 8, scale: 2, default: 0.0
      t.boolean :availability, default: true

      t.timestamps
    end

    add_index :freelancers, :email, unique: true
  end
end
