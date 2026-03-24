class CreateFreelancerSkills < ActiveRecord::Migration[8.1]
  def change
    create_table :freelancer_skills do |t|
      t.references :freelancer, null: false, foreign_key: true
      t.references :skill, null: false, foreign_key: true

      t.timestamps
    end
    add_index :freelancer_skills, %i[freelancer_id skill_id], unique: true  end
end
