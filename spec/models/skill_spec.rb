require 'rails_helper'

RSpec.describe Skill, type: :model do
  describe 'validations' do
    it 'is valid with a name' do
      skill = described_class.new(name: 'Web Development')
      expect(skill).to be_valid
    end

    it 'is invalid without a name' do
      skill = described_class.new(name: nil)
      expect(skill).not_to be_valid
      expect(skill.errors[:name]).to include("can't be blank")
    end

    it 'is invalid with a duplicate name' do
      described_class.create!(name: 'Web Development')
      duplicate = described_class.new(name: 'Web Development')
      expect(duplicate).not_to be_valid
      expect(duplicate.errors[:name]).to include('has already been taken')
    end
  end
end
