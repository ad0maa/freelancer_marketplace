class CreateClients < ActiveRecord::Migration[8.1]
  def change
    create_table :clients do |t|
      t.string :name, null: false
      t.string :email, null: false

      t.timestamps
    end

    add_index :clients, :email, unique: true
  end
end
